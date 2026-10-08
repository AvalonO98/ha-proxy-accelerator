"""Docker 守护进程配置（/etc/docker/daemon.json）读写、校验、应用与回滚。

这是整个插件风险最高的地方：**daemon.json 写坏会让 dockerd 起不来，进而 HA 全挂**。
因此这里的所有写操作都遵守四条铁律：

  1. 只增删自己管理的两个键（registry-mirrors / proxies），其它键原样保留；
  2. 写之前先对完整原文做备份（含"原本是否存在/原本的值"），备份落在 /data；
  3. 原子替换（先写临时文件再 mv），绝不出现半截文件；
  4. 应用后必须用 dockerd 的实时配置（Docker Engine API /info）反过来校验，
     失败则把原文写回去。

另外 registry-mirrors 支持 SIGHUP 热加载（不重启、不中断 HA），
proxies 不支持，必须重启 dockerd —— 重启交给宿主上的独立 systemd 单元执行，
这样插件容器自己被杀掉也不影响回滚逻辑跑完。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from . import dockerapi, hostops, settings
from .logbus import log

DAEMON_JSON = "/etc/docker/daemon.json"
MANAGED_KEYS = ("registry-mirrors", "proxies")
PROXY_KEYS = ("http-proxy", "https-proxy", "no-proxy", "ftp-proxy", "all-proxy")

#: HAOS 上 /etc 位于只读的不可变 rootfs（HAOS 自带的 /etc/docker/daemon.json
#: 就是那个不可变文件），直接写会得到 "Read-only file system"。
#: 社区通行做法：把真正的配置放到可写的持久位置，再用 bind mount 覆盖到
#: /etc/docker/daemon.json。按"持久性优先"排序，逐个尝试：
STORE_CANDIDATES = (
    "/etc/udev/rules.d/netproxy-daemon.json",   # HAOS 明确支持持久化 udev 规则的位置
    "/mnt/overlay/etc/docker/daemon.json",      # hassos-overlay 持久层
    "/mnt/data/netproxy/daemon.json",           # 数据分区，必然可写
    "/run/netproxy-daemon.json",                # tmpfs；重启即失，仅作兜底
)

_AWK_MOUNTED = """awk -v p="$1" '$2==p{f=1} END{exit !f}' /proc/mounts"""

HOST_APPLY_SCRIPT = "/run/netproxy-apply.sh"
HOST_APPLY_BACKUP = "/run/netproxy-daemon-backup.json"
HOST_APPLY_ABSENT = "/run/netproxy-daemon-absent"
HOST_APPLY_RESULT = "/run/netproxy-result.json"

ABSENT_MARKER = "__NETPROXY_ABSENT__"


@dataclass
class Plan:
    """要落到 daemon.json 的托管键。None 表示"不碰这个键"。"""

    registry_mirrors: Optional[list] = None
    proxies: Optional[dict] = None
    need_restart: bool = False
    reason: str = ""
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def target_path() -> str:
    return hostops.hp(DAEMON_JSON)


def read_current() -> dict:
    path = target_path()
    raw = hostops.read_text(path)
    if raw is None:
        return {"existed": False, "raw": None, "data": {}, "ok": True, "path": path}
    try:
        data = json.loads(raw)
    except Exception as e:
        return {"existed": True, "raw": raw, "data": None, "ok": False, "path": path,
                "error": f"现有 daemon.json 不是合法 JSON：{e}"}
    if not isinstance(data, dict):
        return {"existed": True, "raw": raw, "data": None, "ok": False, "path": path,
                "error": "现有 daemon.json 顶层不是对象"}
    return {"existed": True, "raw": raw, "data": data, "ok": True, "path": path}


def original_managed(current_data: dict) -> dict:
    """记录托管键的"原值"，用于关闭插件时精确还原。"""
    out = {}
    for k in MANAGED_KEYS:
        if k in (current_data or {}):
            out[k] = {"present": True, "value": current_data[k]}
        else:
            out[k] = {"present": False}
    return out


# --------------------------------------------------------------------------- #
# 组装与校验
# --------------------------------------------------------------------------- #
def build(current_data: dict, plan: Plan) -> dict:
    new = dict(current_data or {})
    if plan.registry_mirrors is not None:
        if plan.registry_mirrors:
            new["registry-mirrors"] = list(plan.registry_mirrors)
        else:
            new.pop("registry-mirrors", None)
    if plan.proxies is not None:
        if plan.proxies:
            new["proxies"] = dict(plan.proxies)
        else:
            new.pop("proxies", None)
    return new


def validate(obj: dict) -> tuple:
    if not isinstance(obj, dict):
        return False, "顶层必须是 JSON 对象"

    mirrors = obj.get("registry-mirrors")
    if mirrors is not None:
        if not isinstance(mirrors, list) or not mirrors:
            return False, "registry-mirrors 必须是非空数组"
        for m in mirrors:
            if not isinstance(m, str) or not m.startswith(("http://", "https://")):
                return False, f"registry-mirrors 的元素必须以 http:// 或 https:// 开头：{m!r}"

    proxies = obj.get("proxies")
    if proxies is not None:
        if not isinstance(proxies, dict) or not proxies:
            return False, "proxies 必须是非空对象"
        for k, v in proxies.items():
            if k not in PROXY_KEYS:
                return False, f"proxies 不支持的键：{k}（可用：{', '.join(PROXY_KEYS)}）"
            if not isinstance(v, str):
                return False, f"proxies.{k} 必须是字符串"

    try:
        json.dumps(obj, ensure_ascii=False)
    except Exception as e:
        return False, f"无法序列化：{e}"
    return True, "ok"


def diff_keys(old: dict, new: dict) -> list:
    changed = []
    for k in sorted(set(list((old or {}).keys()) + list(new.keys()))):
        if (old or {}).get(k) != new.get(k):
            changed.append(k)
    return changed


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
def backup(current: dict, label: str = "manual") -> dict:
    settings.ensure_dirs()
    ts = time.strftime("%Y%m%d-%H%M%S")
    bid = f"{ts}-{label}"
    path = settings.BACKUP_DIR / f"daemon-{bid}.json"
    rec = {
        "id": bid,
        "ts": time.time(),
        "label": label,
        "path": current.get("path") or target_path(),
        "existed": bool(current.get("existed")),
        "raw": current.get("raw"),
        "data": current.get("data") if current.get("ok") else None,
        "managed_original": original_managed(current.get("data") or {}),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    log(f"已备份现有 daemon.json → {path.name}", "info", "daemon.json")
    return rec


def list_backups(limit: int = 20) -> list:
    settings.ensure_dirs()
    out = []
    for p in sorted(settings.BACKUP_DIR.glob("daemon-*.json"), reverse=True)[:limit]:
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
            out.append({
                "id": rec.get("id", p.stem),
                "ts": rec.get("ts"),
                "label": rec.get("label"),
                "existed": rec.get("existed"),
                "managed_original": rec.get("managed_original"),
            })
        except Exception:
            continue
    return out


def load_backup(bid: str) -> Optional[dict]:
    settings.ensure_dirs()
    for p in settings.BACKUP_DIR.glob("daemon-*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
            if rec.get("id") == bid or p.stem == f"daemon-{bid}":
                return rec
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# 写（底层，不含重启）
# --------------------------------------------------------------------------- #
def is_mounted(path: str = DAEMON_JSON) -> bool:
    rc, _, _ = hostops.sh_host(_AWK_MOUNTED, [hostops.hp(path)], timeout=15)
    return rc == 0


def mount_candidates() -> list:
    return list(STORE_CANDIDATES)


def _readonly_error(msg: str) -> bool:
    m = (msg or "").lower()
    return "read-only file system" in m or "readonly" in m or "erofs" in m


def _bind_mount(src: str, dst: str) -> tuple:
    if is_mounted(dst):
        hostops.run_host(["umount", hostops.hp(dst)], timeout=20)
    rc, out, err = hostops.run_host(["mount", "--bind", hostops.hp(src), hostops.hp(dst)], timeout=25)
    if rc != 0:
        return False, ((err or out).strip() or f"rc={rc}")[:250]
    if not is_mounted(dst):
        return False, "mount 命令返回成功，但 /proc/mounts 里没有该挂载点"
    return True, "ok"


def release_bind_mount(dst: str = DAEMON_JSON) -> tuple:
    if not is_mounted(dst):
        return True, "未挂载"
    rc, out, err = hostops.run_host(["umount", hostops.hp(dst)], timeout=20)
    if rc != 0:
        return False, ((err or out).strip() or f"rc={rc}")[:250]
    return True, "ok"


def write_daemon_json(obj: dict) -> tuple:
    """写入 daemon.json。返回 (ok, detail)。

    detail 形如 "direct" 或 "bind:/etc/udev/rules.d/netproxy-daemon.json"，
    调用方据此判断是否需要 bind mount 与如何回滚。
    """
    ok, msg = validate(obj)
    if not ok:
        return False, f"拒绝写入非法配置：{msg}"
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"

    # 1) 直接原子写：/etc 可写的环境（例如 Supervised on Debian）走这条
    ok, msg = hostops.write_text(target_path(), text, atomic=True)
    if ok:
        return True, "direct"

    if not _readonly_error(msg):
        return False, msg

    # 2) /etc 只读（HAOS）：写到可写位置再 bind mount 覆盖
    errors = []
    for store in STORE_CANDIDATES:
        ok, m = hostops.write_text(hostops.hp(store), text, atomic=False)
        if not ok:
            errors.append(f"{store}: 写入失败 {m}")
            continue
        ok2, m2 = _bind_mount(store, DAEMON_JSON)
        if not ok2:
            errors.append(f"{store}: bind mount 失败 {m2}")
            continue
        log(f"已用 bind mount 覆盖 {DAEMON_JSON} → {store}", "notice", "daemon.json")
        return True, f"bind:{store}"

    return False, ("/etc/docker/daemon.json 位于只读文件系统，且没有任何可写的落地点可用："
                   + " ｜ ".join(errors[:4]))


def restore_raw(raw: Optional[str], existed: bool) -> tuple:
    """把 daemon.json 恢复成原始状态。

    bind mount 模式下"恢复"就是卸载挂载点——HAOS 自带的那个不可变文件会重新露出来，
    它本来就是原始内容，所以这种方式天然安全（不会破坏 OS 文件）。
    """
    if is_mounted(DAEMON_JSON):
        ok, msg = release_bind_mount(DAEMON_JSON)
        if not ok:
            return False, f"卸载 {DAEMON_JSON} 的 bind mount 失败：{msg}"
        log(f"已卸载 {DAEMON_JSON} 的 bind mount，露出 OS 原始配置", "notice", "daemon.json")
        return True, "ok"
    if not existed:
        return hostops.remove(target_path())
    if raw is None:
        return False, "备份里没有原始内容"
    return hostops.write_text(target_path(), raw, atomic=True)


# --------------------------------------------------------------------------- #
# 热加载 / 重启
# --------------------------------------------------------------------------- #
def sighup_dockerd() -> tuple:
    """SIGHUP 让 dockerd 重读支持热加载的配置（registry-mirrors、insecure-registries 等）。

    host_pid: true 让我们与宿主共享 PID 命名空间，因此可以直接给 dockerd 发信号。
    """
    pids = hostops.find_pids("dockerd")
    if not pids:
        return False, "找不到 dockerd 进程（host_pid 可能未生效）"
    ok_any = False
    errs = []
    for pid in pids:
        ok, msg = hostops.signal_pid(pid, 1)  # SIGHUP
        if ok:
            ok_any = True
        else:
            errs.append(f"pid {pid}: {msg}")
    if ok_any:
        return True, f"SIGHUP → dockerd ({', '.join(str(p) for p in pids)})"
    return False, "; ".join(errs) or "SIGHUP 失败"


def _apply_script(timeout: int) -> str:
    return f"""#!/bin/sh
# 由 Net Proxy 插件生成；在宿主机的独立 systemd 单元里运行，
# 这样即使插件容器在 docker 重启过程中被一起杀掉，回滚逻辑仍会执行完。
set -u
TGT={DAEMON_JSON}
BK={HOST_APPLY_BACKUP}
ABSENT={HOST_APPLY_ABSENT}
RESULT={HOST_APPLY_RESULT}
TIMEOUT={int(timeout)}
now() {{ date +%s; }}

healthy() {{
  systemctl is-active --quiet docker 2>/dev/null || return 1
  if command -v docker >/dev/null 2>&1; then
    docker info >/dev/null 2>&1 || return 1
  else
    if command -v pgrep >/dev/null 2>&1; then
      pgrep -x dockerd >/dev/null 2>&1 || return 1
    fi
  fi
  return 0
}}

echo "[netproxy-apply] restarting docker ($(now))"
systemctl restart docker 2>&1 || echo "[netproxy-apply] systemctl restart failed"

ok=0
i=0
while [ "$i" -lt "$TIMEOUT" ]; do
  if healthy; then ok=1; break; fi
  i=$((i+2)); sleep 2
done

if [ "$ok" -ne 1 ]; then
  echo "[netproxy-apply] docker did not become healthy in ${{TIMEOUT}}s -> rolling back"
  if [ -f "$ABSENT" ]; then
    rm -f "$TGT"
  elif [ -f "$BK" ]; then
    cp -f "$BK" "$TGT"
  fi
  systemctl restart docker 2>&1 || true
  i=0
  while [ "$i" -lt 60 ]; do
    healthy && break
    i=$((i+2)); sleep 2
  done
  if healthy; then
    echo "[netproxy-apply] rollback OK, docker healthy"
  else
    echo "[netproxy-apply] ROLLBACK FAILED - docker still unhealthy"
  fi
else
  echo "[netproxy-apply] docker healthy after ${{i}}s"
fi

printf '{{"ok":%s,"finished":%s}}\\n' "$ok" "$(now)" > "$RESULT"
echo "[netproxy-apply] done"
"""


def restart_dockerd_detached(timeout: int, backup_raw: Optional[str] = None,
                             backup_existed: bool = True) -> tuple:
    """写好回滚素材 + 脚本，然后交给宿主 systemd 独立单元重启 dockerd。

    backup_raw/backup_existed 必须是**本次改动之前**的 daemon.json 内容：
    脚本在 dockerd 起不来时会用它覆盖回去。调用方必须在写入新配置之前就把原内容取好。
    """
    if backup_existed and backup_raw is not None:
        hostops.remove(hostops.hp(HOST_APPLY_ABSENT))
        ok, msg = hostops.write_text(hostops.hp(HOST_APPLY_BACKUP), backup_raw, atomic=False)
        if not ok:
            return False, f"写入宿主备份失败：{msg}"
    else:
        hostops.remove(hostops.hp(HOST_APPLY_BACKUP))
        hostops.write_text(hostops.hp(HOST_APPLY_ABSENT), "1\n", atomic=False)

    hostops.remove(hostops.hp(HOST_APPLY_RESULT))
    ok, msg = hostops.write_text(hostops.hp(HOST_APPLY_SCRIPT), _apply_script(timeout), atomic=False)
    if not ok:
        return False, f"写入宿主脚本失败：{msg}"

    # 先清理可能残留的同名单元，再启动
    hostops.run_host(["systemctl", "reset-failed", "netproxy-apply.service"], timeout=20)
    rc, out, err = hostops.run_host([
        "systemd-run", "--unit=netproxy-apply", "--collect",
        "--description=Net Proxy: apply docker daemon config",
        "/bin/sh", HOST_APPLY_SCRIPT,
    ], timeout=30)
    if rc != 0:
        return False, f"systemd-run 失败(rc={rc})：{(err or out).strip()[:300]}"
    return True, "已在宿主独立单元 netproxy-apply 中重启 dockerd"


def read_apply_result() -> Optional[dict]:
    raw = hostops.read_text(hostops.hp(HOST_APPLY_RESULT))
    if not raw:
        return None
    try:
        return json.loads(raw.strip())
    except Exception:
        return {"ok": None, "raw": raw.strip()[:200]}


def unit_active() -> Optional[bool]:
    rc, out, _ = hostops.run_host(["systemctl", "is-active", "netproxy-apply.service"], timeout=15)
    state = out.strip()
    if not state:
        return None
    return state == "active"


# --------------------------------------------------------------------------- #
# 校验（真实生效）
# --------------------------------------------------------------------------- #
def verify(expected_mirrors: Optional[list], expected_proxies: Optional[dict]) -> dict:
    """用 dockerd 的实时配置反过来校验。"""
    s = dockerapi.summarize()
    if not s.get("available"):
        return {"ok": False, "error": s.get("error"), "detail": "无法读取 dockerd 实时配置"}

    problems = []
    if expected_mirrors is not None:
        live = sorted(s.get("registry_mirrors") or [])
        want = sorted(expected_mirrors)
        if live != want:
            problems.append(f"registry-mirrors 实时值 {live} 与期望 {want} 不一致")
    if expected_proxies:
        live_http = (s.get("http_proxy") or "").rstrip("/")
        want_http = (expected_proxies.get("http-proxy") or "").rstrip("/")
        if live_http != want_http:
            problems.append(f"HttpProxy 实时值 {live_http!r} 与期望 {want_http!r} 不一致")
        live_https = (s.get("https_proxy") or "").rstrip("/")
        want_https = (expected_proxies.get("https-proxy") or "").rstrip("/")
        if live_https != want_https:
            problems.append(f"HttpsProxy 实时值 {live_https!r} 与期望 {want_https!r} 不一致")
    elif expected_proxies == {}:
        if s.get("http_proxy") or s.get("https_proxy"):
            problems.append("期望清空代理，但实时值仍有代理")

    return {"ok": not problems, "problems": problems, "live": {
        "registry_mirrors": s.get("registry_mirrors"),
        "http_proxy": s.get("http_proxy"),
        "https_proxy": s.get("https_proxy"),
        "no_proxy": s.get("no_proxy"),
    }}
