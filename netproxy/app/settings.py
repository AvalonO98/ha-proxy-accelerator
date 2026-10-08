"""配置与状态存储。

配置来源优先级：
  1. /data/netproxy/config.json —— 面板里改过的值（运行时真相）
  2. /data/options.json        —— Supervisor 按 config.yaml 的 options 渲染的初始值
  3. DEFAULTS                  —— 文件缺失（本地开发/灾后）时的兜底

设计要点：面板写入的值全部落在 /data，不依赖 Supervisor 选项回写，
因此即使 Supervisor API 不可用，插件依然自洽可用。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("NETPROXY_DATA", "/data"))
STATE_DIR = DATA_DIR / "netproxy"
CONFIG_FILE = STATE_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"
OPTIONS_FILE = DATA_DIR / "options.json"
BACKUP_DIR = STATE_DIR / "backups"
RUN_DIR = STATE_DIR / "run"
BIN_DIR = STATE_DIR / "bin"
TOKEN_FILE = STATE_DIR / "api_token"

MODES = ("docker_mirror", "upstream_proxy", "wireguard")
UPSTREAM_TYPES = ("none", "http", "socks5")

DEFAULTS = {
    "enabled": False,
    "mode": "docker_mirror",
    "mirrors": ["https://docker.m.daocloud.io", "https://docker.1ms.run"],
    "github_mirrors": ["https://ghfast.top/"],
    "upstream": {"type": "none", "url": "", "username": "", "password": ""},
    "subscription": {"url": "", "name": ""},
    "wireguard": {"config": ""},
    "kernel_url": "",
    "kernel_version": "",
    "dns": ["223.5.5.5", "119.29.29.29"],
    "auto_rollback": True,
    "health_check_seconds": 300,
    "apply_timeout_seconds": 180,
    "log_level": "info",
}

_lock = threading.RLock()


def ensure_dirs() -> None:
    for d in (STATE_DIR, BACKUP_DIR, RUN_DIR, BIN_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def normalize(cfg: dict) -> dict:
    """把任意来源的配置收敛成合法结构，绝不抛出。"""
    cfg = _deep_merge(DEFAULTS, cfg if isinstance(cfg, dict) else {})

    if cfg.get("mode") not in MODES:
        cfg["mode"] = DEFAULTS["mode"]
    up = cfg.get("upstream")
    if not isinstance(up, dict):
        up = dict(DEFAULTS["upstream"])
    if up.get("type") not in UPSTREAM_TYPES:
        up["type"] = "none"
    cfg["upstream"] = up
    if not isinstance(cfg.get("subscription"), dict):
        cfg["subscription"] = dict(DEFAULTS["subscription"])
    if not isinstance(cfg.get("wireguard"), dict):
        cfg["wireguard"] = dict(DEFAULTS["wireguard"])

    mirrors = cfg.get("mirrors")
    if isinstance(mirrors, str):
        mirrors = [mirrors]
    if not isinstance(mirrors, list):
        mirrors = []
    cfg["mirrors"] = [str(m).strip() for m in mirrors if str(m).strip()]

    # GitHub 加速前缀：允许任意自定义条目（前缀式或含 {url} 的模板）
    ghm = cfg.get("github_mirrors")
    if isinstance(ghm, str):
        ghm = [ghm]
    if not isinstance(ghm, list):
        ghm = []
    cfg["github_mirrors"] = [str(m).strip() for m in ghm if str(m).strip()]
    cfg["kernel_version"] = str(cfg.get("kernel_version") or "").strip()

    dns = cfg.get("dns")
    if isinstance(dns, str):
        dns = [dns]
    if not isinstance(dns, list) or not dns:
        dns = list(DEFAULTS["dns"])
    cfg["dns"] = [str(d).strip() for d in dns if str(d).strip()]

    for k in ("enabled", "auto_rollback"):
        cfg[k] = bool(cfg.get(k))
    for k in ("health_check_seconds",):
        try:
            cfg[k] = max(30, min(3600, int(cfg.get(k, DEFAULTS[k]))))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    try:
        cfg["apply_timeout_seconds"] = max(30, min(900, int(cfg.get("apply_timeout_seconds", 180))))
    except (TypeError, ValueError):
        cfg["apply_timeout_seconds"] = DEFAULTS["apply_timeout_seconds"]
    if cfg.get("log_level") not in ("trace", "debug", "info", "notice", "warning", "error", "fatal"):
        cfg["log_level"] = "info"
    return cfg


def _read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path: Path, obj) -> None:
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config() -> dict:
    with _lock:
        cfg = None
        src = "defaults"
        if CONFIG_FILE.exists():
            cfg = _read_json(CONFIG_FILE)
            src = "config.json"
        if cfg is None:
            cfg = _read_json(OPTIONS_FILE)
            if cfg is not None:
                src = "options.json"
        out = normalize(cfg)
        out["_source"] = src
        return out


def save_config(cfg: dict) -> dict:
    with _lock:
        clean = normalize(cfg)
        clean.pop("_source", None)
        _write_json(CONFIG_FILE, clean)
        return clean


def load_state() -> dict:
    with _lock:
        st = _read_json(STATE_FILE)
        if not isinstance(st, dict):
            st = {}
        st.setdefault("applied", {})       # 当前已落到 daemon.json 的内容描述
        st.setdefault("last_job", None)
        st.setdefault("history", [])       # 配置变更历史
        return st


def save_state(st: dict) -> dict:
    with _lock:
        _write_json(STATE_FILE, st)
        return st


def push_history(kind: str, detail: str, extra: dict | None = None) -> dict:
    """记录一次配置变更（面板可一键回滚）。"""
    st = load_state()
    rec = {
        "ts": round(time.time(), 3),
        "kind": kind,
        "detail": detail,
        "extra": extra or {},
    }
    hist = st.get("history") or []
    hist.insert(0, rec)
    st["history"] = hist[:50]
    save_state(st)
    return rec


def api_token() -> str:
    """直连端口（非 ingress）访问时需要的一次性令牌。"""
    with _lock:
        ensure_dirs()
        if TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if t:
                return t
        import secrets

        t = secrets.token_urlsafe(24)
        TOKEN_FILE.write_text(t, encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
        return t


SECRET_PATHS = (("upstream", "password"), ("wireguard", "config"))


def mask(cfg: dict) -> dict:
    """日志用：隐去密钥类字段。"""
    import copy

    out = copy.deepcopy(cfg)
    for path in SECRET_PATHS:
        cur = out
        for k in path[:-1]:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, dict) and cur.get(path[-1]):
            v = str(cur[path[-1]])
            cur[path[-1]] = f"***({len(v)} chars)"
    return out
