#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HACS 官方组件加速补丁模块 (hacs_patch.py)
========================================
思路来源: 社区「HACS 极速版 / HACS China」等项目的应用层加速方法 ——
把 custom_components/hacs 内写死的 GitHub 下载地址改写为
「GitHub 反代前缀 + 原地址」(例如 https://ghfast.top/https://github.com/...)，
使 HACS 的版本检查 / 下载 / 前端更新流量先经过国内可达的反代。

安全设计:
  * 修改前把将改动的文件**整份备份**到 custom_components/.hacs-accel-backup/
  * 关闭开关 = 从备份逐文件还原, 可随时回滚
  * 每次 status() 会检测"补丁是否被 HACS 自身更新覆盖", 若被覆盖会提示重新应用
  * 应用后需重启 Home Assistant 才生效(模块只改文件, 不碰运行态)

对外接口(被 server.py 的 /api/hacs/* 调用):
    status()          -> dict
    apply(enabled, prefix=None) -> dict
"""
import fnmatch
import json
import os
import re
import shutil
import time

HACS_DIR = os.environ.get("HACS_DIR", "/config/custom_components/hacs")
BACKUP_DIR = os.environ.get(
    "HACS_BACKUP_DIR", "/config/custom_components/.hacs-accel-backup")
MARKER = os.path.join(BACKUP_DIR, "hacs-accel.json")

# 需要替换为反代前缀的 GitHub 相关域名(scheme+host 完整匹配, 避免误伤)
DOMAIN_RE = (
    r"https://(?:"
    r"api\.github\.com|"
    r"raw\.githubusercontent\.com|"
    r"objects\.githubusercontent\.com|"
    r"github-releases\.githubusercontent\.com|"
    r"codeload\.github\.com|"
    r"avatars\.githubusercontent\.com|"
    r"user-images\.githubusercontent\.com|"
    r"gist\.github\.com|"
    r"www\.github\.com|"
    r"github\.com"
    r")"
)
PATCH_RE = re.compile(r"(?P<url>" + DOMAIN_RE + r")(?=/|[\"'\s]|$)")
# 参与扫描/替换的文件后缀
EXTENSIONS = ("*.py", "*.js", "*.json", "*.html", "*.txt")

DEFAULT_PREFIX = "https://ghfast.top/"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _iter_target_files(root):
    """遍历 hacs 目录下所有可补丁文件(排除备份目录与隐藏目录)。"""
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != "__pycache__"]
        for name in filenames:
            if any(fnmatch.fnmatch(name, ext) for ext in EXTENSIONS):
                yield os.path.join(dirpath, name)


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _prefix_clean(prefix):
    prefix = (prefix or "").strip()
    if not prefix:
        prefix = DEFAULT_PREFIX
    if not prefix.startswith(("http://", "https://")):
        prefix = "https://" + prefix
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------

def _hacs_manifest():
    path = os.path.join(HACS_DIR, "manifest.json")
    data = _read_bytes(path)
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _read_marker():
    data = _read_bytes(MARKER)
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _count_patched_files(marker):
    """统计当前仍处于"已补丁"状态的文件数(与备份对比)。"""
    if not marker:
        return 0, 0
    total = len(marker.get("files", []))
    changed = 0
    for rel in marker.get("files", []):
        cur = _read_bytes(os.path.join(HACS_DIR, rel))
        bak = _read_bytes(os.path.join(BACKUP_DIR, "files", rel))
        if cur is not None and bak is not None and cur != bak:
            changed += 1
    return changed, total


def status():
    """返回 HACS 与补丁状态(供面板展示)。"""
    out = {
        "hacs_installed": False,
        "version": None,
        "patched": False,
        "backup_exists": os.path.isdir(BACKUP_DIR),
        "prefix": None,
        "note": None,
        "files_patched": 0,
        "files_total": 0,
    }
    manifest = _hacs_manifest()
    if not manifest:
        out["note"] = "未在 custom_components/hacs 找到官方 HACS(manifest.json 缺失)。"
        return out
    out["hacs_installed"] = True
    out["version"] = manifest.get("version") or manifest.get("hacs_version")

    marker = _read_marker()
    if marker:
        out["prefix"] = marker.get("prefix")
        changed, total = _count_patched_files(marker)
        out["files_patched"] = changed
        out["files_total"] = total
        if changed >= total and total > 0:
            out["patched"] = True
        elif total > 0:
            # 补丁被覆盖(HACS 自更新等)或已被手动还原
            out["patched"] = False
            out["note"] = ("补丁文件与备份不一致(%d/%d), 疑似被 HACS 自更新覆盖"
                           " —— 请点击「应用加速补丁」重新应用。"
                           % (changed, total))
        else:
            out["patched"] = False
            out["note"] = "备份标记存在但没有记录任何改动文件。"
    return out


# --------------------------------------------------------------------------
# 应用 / 回滚
# --------------------------------------------------------------------------

def apply(enabled, prefix=None):
    """enabled=True 打补丁; enabled=False 回滚。prefix 缺省时读面板设置。"""
    if not os.path.isdir(HACS_DIR):
        return {"ok": False, "error": "custom_components/hacs 不存在, 请先安装官方 HACS"}

    if not enabled:
        return _rollback()

    prefix = _prefix_clean(prefix or _settings_prefix())
    marker = _read_marker()

    # 1) 备份将被改动的文件(已有备份则跳过)
    os.makedirs(os.path.join(BACKUP_DIR, "files"), exist_ok=True)
    changed_files = {}
    for path in _iter_target_files(HACS_DIR):
        raw = _read_bytes(path)
        if raw is None:
            continue
        text = raw.decode("utf-8", errors="ignore")
        if not PATCH_RE.search(text):
            continue
        rel = os.path.relpath(path, HACS_DIR)
        bak = os.path.join(BACKUP_DIR, "files", rel)
        if not os.path.exists(bak):
            _write_bytes(bak, raw)
        # 统计将替换的数量
        n = len(PATCH_RE.findall(text))
        changed_files[rel] = n

    if not changed_files:
        # 没有可改文件: 若之前补丁过则可能是已回滚; 提示即可
        if marker:
            return {"ok": True, "applied": True, "changed": 0,
                    "note": "没有发现新的可改写文件; 已存在补丁标记。"}
        return {"ok": False, "error": "HACS 目录中未发现 GitHub 地址(版本过新或结构变化?)",
                "applied": False}

    # 2) 执行替换(逐文件, 出错自动回滚)
    applied = []
    try:
        for rel, count in changed_files.items():
            path = os.path.join(HACS_DIR, rel)
            raw = _read_bytes(path)
            text = raw.decode("utf-8", errors="ignore")

            def _sub(m):
                return prefix + m.group("url")

            new_text, n = PATCH_RE.subn(_sub, text)
            if n:
                _write_bytes(path, new_text.encode("utf-8"))
                applied.append({"file": rel, "count": n})
    except Exception as e:
        _rollback()
        return {"ok": False, "applied": False,
                "error": "打补丁失败已自动回滚: %s" % e}

    # 3) 记录标记(便于状态检测与回滚)
    marker = {
        "prefix": prefix,
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": sorted(changed_files.keys()),
    }
    _write_bytes(MARKER, json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8"))

    total = sum(item["count"] for item in applied)
    return {"ok": True, "applied": True,
            "changed": len(applied), "total_replacements": total,
            "prefix": prefix,
            "note": ("已改写 %d 个文件(%d 处 GitHub 地址)。请重启 Home Assistant "
                     "使 HACS 生效; HACS 自更新后若提示不一致请重新应用。"
                     % (len(applied), total))}


def _rollback():
    marker = _read_marker()
    restored = 0
    if marker:
        for rel in marker.get("files", []):
            bak = os.path.join(BACKUP_DIR, "files", rel)
            if os.path.isfile(bak):
                dst = os.path.join(HACS_DIR, rel)
                if os.path.dirname(dst) and not os.path.isdir(os.path.dirname(dst)):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(bak, dst)
                restored += 1
        try:
            os.remove(MARKER)
        except OSError:
            pass
    return {"ok": True, "applied": False, "restored": restored,
            "note": "已从备份还原 %d 个文件(重启 HA 后 HACS 恢复官方行为)。" % restored}


def _settings_prefix():
    try:
        with open("/data/settings.json", "r", encoding="utf-8") as f:
            st = json.load(f)
        return st.get("hacs_github_proxy") or DEFAULT_PREFIX
    except Exception:
        return DEFAULT_PREFIX


if __name__ == "__main__":
    # 命令行自检: python3 hacs_patch.py status|apply|rollback [prefix]
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif action in ("apply", "on"):
        prefix = sys.argv[2] if len(sys.argv) > 2 else None
        print(json.dumps(apply(True, prefix), ensure_ascii=False, indent=2))
    elif action in ("rollback", "off"):
        print(json.dumps(apply(False), ensure_ascii=False, indent=2))
    else:
        print("usage: hacs_patch.py status|apply [prefix]|rollback")
