"""mihomo（Clash.Meta）内核：安装、配置生成、进程管理、连通性测试。

为什么选 mihomo 一个内核同时覆盖方式 B 和 C：
  * 方式 B：它有 mixed-port（HTTP+SOCKS5 入口），上游可以是 http/socks5，
    也支持 Clash/mihomo 订阅（proxy-providers）；
  * 方式 C：它内置 **userspace WireGuard** 出站，不需要内核模块、不需要
    wg-quick、不建 tun、不改宿主机路由 —— 因此不会出现"全局 VPN 把
    Supervisor ↔ 容器通信掐断"的经典事故。

后端只用 Python 标准库：YAML 由本模块手工生成（不做通用 YAML 序列化，
配置结构完全受控，避免把用户输入拼成任意 YAML）。
"""
from __future__ import annotations

import gzip
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from . import settings, wg
from .logbus import log

PROXY_PORT = 7890
CTRL_PORT = 9090

_BUNDLED = "/opt/netproxy/bin/mihomo"
_SHARE = "/share/netproxy/mihomo"

_proc: Optional[subprocess.Popen] = None
_reader: Optional[threading.Thread] = None
_last_error = ""
_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# 路径与安装
# --------------------------------------------------------------------------- #
def mihomo_dir() -> str:
    d = settings.STATE_DIR / "mihomo"
    (d / "providers").mkdir(parents=True, exist_ok=True)
    return str(d)


def config_path() -> str:
    return os.path.join(mihomo_dir(), "config.yaml")


def kernel_path() -> Optional[str]:
    """按优先级找一个可执行内核：手动放置的 → 已下载的 → 镜像内置的。"""
    for p in (_SHARE, str(settings.BIN_DIR / "mihomo"), _BUNDLED):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # 兼容用户放进 /share/netproxy/ 的其它命名
    for name in ("mihomo", "clash-meta", "Clash.Meta", "clash"):
        p = os.path.join("/share/netproxy", name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def kernel_version(path: Optional[str] = None) -> str:
    p = path or kernel_path()
    if not p:
        return ""
    try:
        r = subprocess.run([p, "-v"], capture_output=True, timeout=15)
        out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        return out.strip().splitlines()[0] if out.strip() else ""
    except Exception:
        return ""


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv7") or m.startswith("armv6"):
        return "armv7"
    return m


GH_REPO = "MetaCubeX/mihomo"
GH_API = f"https://api.github.com/repos/{GH_REPO}/releases/latest"
GH_DL = "https://github.com/{repo}/releases/download/{tag}/{asset}"


def _asset_names(tag: str) -> list:
    arch = _arch()
    if arch == "amd64":
        # 优先 compatible 版本：兼容老 CPU
        return [f"mihomo-linux-amd64-compatible-{tag}.gz", f"mihomo-linux-amd64-{tag}.gz"]
    if arch == "arm64":
        return [f"mihomo-linux-arm64-{tag}.gz"]
    if arch == "armv7":
        return [f"mihomo-linux-armv7-{tag}.gz", f"mihomo-linux-armv6-{tag}.gz"]
    return [f"mihomo-linux-{arch}-{tag}.gz"]


def latest_tag() -> tuple:
    """查 GitHub 最新 release tag。"""
    try:
        req = urllib.request.Request(GH_API, headers={"User-Agent": "netproxy-addon"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = (data.get("tag_name") or "").strip()
        if tag:
            return True, tag
        return False, "GitHub API 未返回 tag_name"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def official_asset_url(version: str = "") -> tuple:
    """官方资源地址。返回 (ok, tag, url, err)。"""
    tag = (version or "").strip()
    if tag and not tag.startswith("v"):
        tag = "v" + tag
    if not tag:
        ok, res = latest_tag()
        if not ok:
            return False, "", "", (f"无法确定最新版本（{res}）；"
                                  "可在「镜像源」里填写内核下载地址，或指定 mihomo 内核版本")
        tag = res
    asset = _asset_names(tag)[0]
    return True, tag, GH_DL.format(repo=GH_REPO, tag=tag, asset=asset), ""


def apply_mirrors(url: str, mirrors) -> list:
    """把 GitHub 加速前缀作用到官方地址上。返回 [(结果URL, 使用的加速源)]。

    支持两种写法：
      * 前缀式：https://ghfast.top/            → https://ghfast.top/https://github.com/...
      * 模板式：https://x/proxy?url={url}      → {url} 被替换成官方地址
    """
    out = []
    for m in mirrors or []:
        m = str(m or "").strip()
        if not m:
            continue
        if "{url}" in m:
            out.append((m.replace("{url}", url), m))
        else:
            base = m if m.startswith("http") else "https://" + m
            out.append((base.rstrip("/") + "/" + url, m))
    return out


def candidate_urls(explicit: str = "", mirrors=None, version: str = "") -> list:
    """按优先级列出所有下载候选：自定义地址 → GitHub 直连 → 各加速源。"""
    cands = []
    explicit = (explicit or "").strip()
    if explicit:
        cands.append(("自定义地址", explicit))
    ok, tag, official, err = official_asset_url(version)
    if ok:
        cands.append((f"GitHub 直连 {tag}", official))
        for u, m in apply_mirrors(official, mirrors):
            cands.append((f"加速源 {m}", u))
    else:
        log(err, "warning", "mihomo")
    return cands


def _download(url: str, dest: str, progress=None) -> tuple:
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netproxy-addon"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    except Exception as e:
        _safe_remove(tmp)
        return False, f"{type(e).__name__}: {e}"

    try:
        with open(tmp, "rb") as f:
            magic = f.read(2)
        if magic == b"\x1f\x8b":
            out = dest + ".raw"
            with gzip.open(tmp, "rb") as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            _safe_remove(tmp)
            os.replace(out, dest)
        else:
            os.replace(tmp, dest)
        os.chmod(dest, 0o755)
    except Exception as e:
        _safe_remove(tmp)
        return False, f"解压/安装失败：{type(e).__name__}: {e}"
    return True, "ok"


def install(explicit_url: str = "", mirrors=None, version: str = "", progress=None) -> tuple:
    """下载并安装内核到 /data/netproxy/bin/mihomo，多源自动回退。"""
    cands = candidate_urls(explicit_url, mirrors, version)
    if not cands:
        return False, "没有可用的下载源：请填写内核下载地址，或让 GitHub API 可达"

    dest = str(settings.BIN_DIR / "mihomo")
    errors = []
    for i, (label, url) in enumerate(cands, 1):
        if not url:
            continue
        log(f"下载内核 [{i}/{len(cands)}] {label}", "notice", "mihomo")
        log(f"  URL: {url}", "debug", "mihomo")
        ok, msg = _download(url, dest, progress)
        if not ok:
            errors.append(f"{label}: {msg}")
            log(f"该源失败：{msg}", "warning", "mihomo")
            continue
        ver = kernel_version(dest)
        if not ver:
            _safe_remove(dest)
            errors.append(f"{label}: 下载成功但无法执行（架构不匹配？）")
            continue
        log(f"mihomo 内核就绪：{ver}（来源：{label}）", "notice", "mihomo")
        return True, f"{ver}（来源：{label}）"

    return False, "所有下载源都失败：" + " ｜ ".join(errors[:6])


def _safe_remove(p: str) -> None:
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 配置生成
# --------------------------------------------------------------------------- #
def _y(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _block(lines: list, items: list, indent: int) -> None:
    pad = "  " * indent
    for it in items:
        if isinstance(it, dict):
            first = True
            for k, v in it.items():
                if isinstance(v, list):
                    if first:
                        lines.append(f"{pad}- {k}:")
                        first = False
                    else:
                        lines.append(f"{pad}  {k}:")
                    _block(lines, v, indent + 2)
                elif isinstance(v, dict):
                    lines.append(f"{pad}{'- ' if first else '  '}{k}:")
                    first = False
                    for kk, vv in v.items():
                        lines.append(f"{pad}    {kk}: {_y(vv)}")
                else:
                    lines.append(f"{pad}{'- ' if first else '  '}{k}: {_y(v)}")
                    first = False
        else:
            lines.append(f"{pad}- {_y(it)}")


def _rules() -> list:
    """本地/内网必须直连，否则会把自己代理掉（自锁）或绕远路。"""
    return [
        "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,172.30.32.0/23,DIRECT,no-resolve",   # HA docker 网络（Supervisor/容器）
        "IP-CIDR,172.17.0.0/16,DIRECT,no-resolve",
        "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
        "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",    # CGNAT/Tailscale
        "DOMAIN-SUFFIX,local,DIRECT",
        "DOMAIN-SUFFIX,local.hass.io,DIRECT",
        "DOMAIN-SUFFIX,lan,DIRECT",
        "MATCH,PROXY",
    ]


def generate_config(cfg: dict) -> tuple:
    """根据配置生成 mihomo config.yaml 文本。返回 (text, error)。"""
    mode = cfg["mode"]
    up = cfg.get("upstream") or {}
    sub = cfg.get("subscription") or {}

    proxies: list = []
    provider = None
    groups: list = []
    used = []

    if mode == "wireguard":
        text = (cfg.get("wireguard") or {}).get("config", "")
        try:
            parsed = wg.parse(text)
        except wg.WgError as e:
            return "", f"WireGuard 配置无法解析：{e}"
        ob = wg.to_mihomo_outbound("WG", parsed)
        proxies.append(ob)
        used.append("WireGuard " + f"{parsed['peers'][0]['endpoint_host']}:{parsed['peers'][0]['endpoint_port']}")
    elif mode == "upstream_proxy":
        if up.get("type") in ("http", "socks5") and up.get("url"):
            host, port, url_user, url_pass, err = _parse_upstream(up["url"])
            if err:
                return "", f"上游代理地址无法解析：{err}"
            p = {"name": "UPSTREAM", "type": up["type"], "server": host, "port": port}
            # 面板里单独填的凭据优先；没填则用 URL 里内嵌的
            user = up.get("username") or url_user
            password = up.get("password") or url_pass
            if user:
                p["username"] = user
            if password:
                p["password"] = password
            proxies.append(p)
            used.append(f'{up["type"]}://{host}:{port}')
        if sub.get("url"):
            provider = {
                "sub": {
                    "type": "http",
                    "url": sub["url"],
                    "path": "./providers/sub.yaml",
                    "interval": 86400,
                    "health-check": {"enable": True,
                                     "url": "https://www.gstatic.com/generate_204",
                                     "interval": 300},
                }
            }
            used.append("订阅 " + sub["url"])
        elif os.path.isfile("/share/netproxy/subscription.yaml"):
            provider = {
                "sub": {
                    "type": "file",
                    "path": "/share/netproxy/subscription.yaml",
                    "health-check": {"enable": True,
                                     "url": "https://www.gstatic.com/generate_204",
                                     "interval": 300},
                }
            }
            used.append("本地订阅文件 /share/netproxy/subscription.yaml")

        if not proxies and not provider and up.get("type") != "none":
            return "", ("方式 B 需要填写上游代理地址（http/socks5）、订阅地址，"
                        "或把上游类型选为「直连出口」")
    else:
        return "", f"方式 {mode} 不需要本地内核"

    group_members = [p["name"] for p in proxies]
    g = {"name": "PROXY", "type": "select", "proxies": group_members + ["DIRECT"]}
    if provider:
        g["use"] = list(provider.keys())
    groups.append(g)

    lines = [
        "# 由 Net Proxy 插件自动生成，请勿手工编辑（会被覆盖）",
        f"# generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"mixed-port: {PROXY_PORT}",
        'bind-address: "127.0.0.1"',
        "allow-lan: false",
        'mode: "rule"',
        f'log-level: {_y(cfg.get("log_level", "info"))}',
        "ipv6: false",
        "unified-delay: true",
        "tcp-concurrent: true",
        f'external-controller: "127.0.0.1:{CTRL_PORT}"',
        "dns:",
        "  enable: true",
        "  ipv6: false",
        '  enhanced-mode: "normal"',
        "  nameserver:",
    ]
    for d in cfg.get("dns") or ["223.5.5.5"]:
        lines.append(f"    - {_y(d)}")
    lines.append("  use-hosts: true")

    if provider:
        lines.append("proxy-providers:")
        for k, v in provider.items():
            lines.append(f"  {_y(k)}:")
            for kk, vv in v.items():
                if isinstance(vv, dict):
                    lines.append(f"    {kk}:")
                    for k3, v3 in vv.items():
                        lines.append(f"      {k3}: {_y(v3)}")
                else:
                    lines.append(f"    {kk}: {_y(vv)}")

    lines.append("proxies:")
    if proxies:
        _block(lines, proxies, 1)
    else:
        lines[-1] = "proxies: []"

    lines.append("proxy-groups:")
    _block(lines, groups, 1)
    lines.append("rules:")
    for r in _rules():
        lines.append(f"  - {_y(r)}")

    return "\n".join(lines) + "\n", ""


def _parse_upstream(url: str) -> tuple:
    """解析上游地址，兼容 host:port 与 scheme://[user:pass@]host:port。

    返回 (host, port, url_user, url_pass, err)。凭据写在 URL 里也能识别，
    避免用户只贴 socks5://user:pass@host:port 时认证信息被丢掉。
    """
    s = (url or "").strip()
    if not s:
        return "", 0, "", "", "为空"
    url_user = url_pass = ""
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        cred, s = s.rsplit("@", 1)
        if ":" in cred:
            url_user, url_pass = cred.split(":", 1)
        else:
            url_user = cred
    s = s.split("/", 1)[0]
    if s.count(":") != 1:
        return "", 0, "", "", f"需要 host:port 形式，收到 {url!r}"
    host, port = s.split(":")
    try:
        return host, int(port), url_user, url_pass, ""
    except ValueError:
        return "", 0, "", "", f"端口不是数字：{port!r}"


def _split_hostport(url: str) -> tuple:
    """兼容旧签名：返回 (host, port, err)。"""
    host, port, _u, _p, err = _parse_upstream(url)
    return host, port, err


# --------------------------------------------------------------------------- #
# 进程管理
# --------------------------------------------------------------------------- #
def write_config(cfg: dict) -> tuple:
    text, err = generate_config(cfg)
    if err:
        return False, err
    with open(config_path(), "w", encoding="utf-8") as f:
        f.write(text)
    return True, text


def test_config() -> tuple:
    k = kernel_path()
    if not k:
        return False, "内核未安装"
    try:
        r = subprocess.run([k, "-t", "-d", mihomo_dir(), "-f", config_path()],
                           capture_output=True, timeout=60)
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
        return r.returncode == 0, out[-600:] or f"rc={r.returncode}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def alive() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def start() -> tuple:
    global _proc, _reader, _last_error
    k = kernel_path()
    if not k:
        return False, "内核未安装（请在面板里安装，或把 mihomo 放到 /share/netproxy/mihomo）"
    if alive():
        return True, "已在运行"
    ok, msg = test_config()
    if not ok:
        _last_error = msg
        return False, f"配置校验失败：{msg}"
    try:
        _proc = subprocess.Popen(
            [k, "-d", mihomo_dir(), "-f", config_path()],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=mihomo_dir(),
        )
    except Exception as e:
        _last_error = str(e)
        return False, f"启动失败：{type(e).__name__}: {e}"

    def _pump(proc):
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log(line, "debug", "mihomo")
        except Exception:
            pass

    _reader = threading.Thread(target=_pump, args=(_proc,), name="mihomo-log", daemon=True)
    _reader.start()

    # 等端口监听
    for _ in range(40):
        if not alive():
            _last_error = "进程已退出"
            return False, f"内核启动后立即退出：{_last_error}"
        if _port_open("127.0.0.1", PROXY_PORT):
            log(f"mihomo 已监听 127.0.0.1:{PROXY_PORT}", "notice", "mihomo")
            return True, "ok"
        time.sleep(0.25)
    return False, f"内核启动了但 {PROXY_PORT} 端口未监听"


def stop() -> tuple:
    global _proc
    with _lock:
        if _proc is None:
            return True, "未在运行"
        p = _proc
        _proc = None
    try:
        p.terminate()
        for _ in range(20):
            if p.poll() is not None:
                break
            time.sleep(0.1)
        if p.poll() is None:
            p.kill()
        return True, "已停止"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def status() -> dict:
    k = kernel_path()
    return {
        "installed": bool(k),
        "path": k or "",
        "version": kernel_version(k) if k else "",
        "running": alive(),
        "pid": (_proc.pid if alive() else None),
        "listen_port": PROXY_PORT,
        "listen_ok": _port_open("127.0.0.1", PROXY_PORT),
        "controller_ok": _port_open("127.0.0.1", CTRL_PORT),
        "config_path": config_path(),
        "config_exists": os.path.exists(config_path()),
        "last_error": _last_error,
        "share_hint": _SHARE,
    }


# --------------------------------------------------------------------------- #
# 连通性测试
# --------------------------------------------------------------------------- #
def ctrl_get(path: str, timeout: float = 10.0):
    url = f"http://127.0.0.1:{CTRL_PORT}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def delay_test(url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> dict:
    """通过 mihomo 控制接口测延迟（证明代理链路真的能用）。"""
    if not alive():
        return {"ok": False, "error": "内核未在运行"}
    res = ctrl_get(f"/proxies/PROXY/delay?timeout={timeout_ms}&url={urllib.parse.quote(url, safe='')}")
    if "_error" in res:
        res2 = ctrl_get(f"/group/PROXY/delay?timeout={timeout_ms}&url={urllib.parse.quote(url, safe='')}")
        res = res2
    if isinstance(res, dict) and res.get("delay"):
        return {"ok": True, "delay_ms": res["delay"], "url": url}
    return {"ok": False, "error": str(res)[:300], "url": url}


def _opener(via_proxy: bool):
    if via_proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{PROXY_PORT}",
            "https": f"http://127.0.0.1:{PROXY_PORT}",
        }))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe_url(url: str, method: str = "HEAD", via_proxy: bool = False,
              timeout: float = 12.0) -> dict:
    """轻量探测"地址是否可达"：先 HEAD，不被支持时退化成 Range GET，不下载正文。"""
    opener = _opener(via_proxy)
    last = "unknown"
    attempts = ((method, {"User-Agent": "netproxy-addon"}),
                ("GET", {"User-Agent": "netproxy-addon", "Range": "bytes=0-0"}))
    for m, headers in attempts:
        req = urllib.request.Request(url, method=m, headers=headers)
        t0 = time.time()
        try:
            with opener.open(req, timeout=timeout) as r:
                return {"ok": True, "status": r.status, "ms": int((time.time() - t0) * 1000),
                        "via_proxy": via_proxy, "url": url, "method": m}
        except urllib.error.HTTPError as e:
            if e.code in (405, 501):
                last = f"HTTP {e.code}（不支持 {m}）"
                continue
            return {"ok": True, "status": e.code, "ms": int((time.time() - t0) * 1000),
                    "via_proxy": via_proxy, "url": url, "method": m}
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            continue
    return {"ok": False, "error": last, "via_proxy": via_proxy, "url": url, "method": method}


def http_fetch(url: str, via_proxy: bool, timeout: float = 15.0) -> dict:
    """直接用 HTTP 请求验证：可对比"走代理"与"直连"的结果。

    401/403/404 之类都算"网络可达"，只要拿到 HTTP 响应就说明链路通。
    """
    opener = _opener(via_proxy)
    req = urllib.request.Request(url, headers={"User-Agent": "netproxy-addon"})
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as r:
            return {"ok": True, "status": r.status, "ms": int((time.time() - t0) * 1000),
                    "via_proxy": via_proxy, "url": url}
    except urllib.error.HTTPError as e:
        return {"ok": True, "status": e.code, "ms": int((time.time() - t0) * 1000),
                "via_proxy": via_proxy, "url": url}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "ms": int((time.time() - t0) * 1000), "via_proxy": via_proxy, "url": url}
