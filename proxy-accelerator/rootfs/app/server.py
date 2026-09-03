#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HA 代理加速器 —— 核心服务 (server.py)
=====================================
职责:
  1. 正向 HTTP(S) 代理入口(支持 CONNECT 隧道 + HTTP 绝对 URI 转发)
  2. 上游自选: direct(直连) / http 代理 / socks5 代理 —— 由面板配置
  3. ACL 域名白名单(默认仅放行 GitHub/GHCR/Docker/PyPI 等, 可切全局)
  4. 面板 API: 开关、改配置、上游与镜像可达性检测
  5. 静态面板页面托管(由 Supervisor Ingress 反代同源访问)

纯 Python 3 标准库实现, 无第三方依赖。
状态文件默认 /data/settings.json (add-on 持久目录)。
"""
import argparse
import asyncio
import json
import mimetypes
import os
import re
import socket
import sys
import time
import urllib.parse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")

DEFAULT_SETTINGS = {
    "enabled": False,            # 总开关: 代理入口是否放行
    "mode": "http",              # 上游方式: direct | http | socks5
    "upstream": "127.0.0.1:7890",# 上游代理地址(host:port 或完整 url)
    "upstream_user": "",
    "upstream_pass": "",
    "acl_mode": "whitelist",     # whitelist | global
    "whitelist": [
        "github.com", "api.github.com", "codeload.github.com",
        "raw.githubusercontent.com", "objects.githubusercontent.com",
        "ghcr.io", "pkg.github.com",
        "registry-1.docker.io", "auth.docker.io",
        "production.cloudflare.docker.com",
        "quay.io", "gcr.io", "k8s.gcr.io", "registry.k8s.io",
        "pypi.org", "files.pythonhosted.org",
        "home-assistant.io", "update.home-assistant.io",
        "www.home-assistant.io", "github-releases.githubusercontent.com",
    ],
    # HACS 官方组件加速(补丁式, 见 hacs_patch 模块): 开关与所选 GitHub 反代前缀
    "hacs_patch_enabled": False,
    "hacs_github_proxy": "https://ghfast.top/",  # 用户可自选
    # 已知镜像列表仅为面板展示与探活; 真正生效的宿主侧配置由"向导"给出
    "docker_hub_mirrors": [
        "https://docker.1ms.run",
        "https://docker.m.daocloud.io",
        "https://hub.rat.dev",
        "https://docker.xuanyuan.me",
        "https://docker.1panel.live",
    ],
    "ghcr_mirrors": [
        "https://ghcr.nju.edu.cn",
        "https://ghcr.m.daocloud.io",
    ],
}

# 兼容字符串形式的遗留配置
_BOOL_KEYS = {"enabled", "hacs_patch_enabled", "acl_mode"}


def default_settings():
    return json.loads(json.dumps(DEFAULT_SETTINGS))


class State:
    """读写 /data/settings.json, 每个连接独立读取以保证面板改动即时生效。"""
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        merged = default_settings()
        if isinstance(data, dict):
            merged.update(data)
        # 归一化
        merged["enabled"] = bool(merged.get("enabled"))
        merged["mode"] = merged.get("mode", "http")
        if merged["mode"] not in ("direct", "http", "socks5"):
            merged["mode"] = "http"
        merged["hacs_patch_enabled"] = bool(merged.get("hacs_patch_enabled"))
        return merged

    def save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def update(self, patch):
        cur = self.load()
        for k, v in patch.items():
            if k in cur and not k.startswith("_"):
                cur[k] = v
        cur["enabled"] = bool(cur.get("enabled"))
        cur["hacs_patch_enabled"] = bool(cur.get("hacs_patch_enabled"))
        self.save(cur)
        return cur


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def split_host_port(addr, default_port=None):
    """'host:port' 或 'http://host:port' -> (host, port)"""
    host, port = addr, default_port
    if "://" in addr:
        parts = urllib.parse.urlsplit(addr if "://" in addr else "//" + addr)
        host = parts.hostname or ""
        port = parts.port or default_port
    elif addr.count(":") == 1:
        h, _, p = addr.rpartition(":")
        if p.isdigit():
            host, port = h, int(p)
    if port is None:
        port = 7890
    return host.strip(), int(port)


def host_matches(hostname, pattern):
    hostname = (hostname or "").lower().rstrip(".")
    pattern = pattern.lower().rstrip(".")
    if hostname == pattern:
        return True
    return hostname.endswith("." + pattern)


def acl_allowed(settings, hostname):
    if settings.get("acl_mode") == "global":
        return True
    for p in settings.get("whitelist", []):
        if host_matches(hostname, p):
            return True
    return False


def _http_status(writer, code, reason, body=b"", ctype=b"text/plain; charset=utf-8"):
    head = ("HTTP/1.1 %d %s\r\nServer: ha-proxy-accelerator\r\n"
            "Content-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
            % (code, reason, ctype.decode(), len(body))).encode("latin-1")
    writer.write(head + body)


# --------------------------------------------------------------------------
# 上游连接
# --------------------------------------------------------------------------

class UpstreamConn:
    """封装到上游的已建立连接(按 settings 选择直连 / http / socks5)。"""

    def __init__(self, settings):
        self.settings = settings
        self.mode = settings.get("mode", "http")
        self.remote_host, self.remote_port = None, None
        if self.mode != "direct":
            self.remote_host, self.remote_port = split_host_port(
                settings.get("upstream") or "", 7890)

    async def connect_target(self, host, port):
        """为 CONNECT/转发建立到目标的通道: 返回 (reader, writer, raw_first_bytes)。"""
        if self.mode == "direct":
            return await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=15)
        if self.mode == "http":
            return await asyncio.wait_for(
                asyncio.open_connection(self.remote_host, self.remote_port),
                timeout=15)
        if self.mode == "socks5":
            return await self._socks5_connect(host, port)
        raise RuntimeError("bad mode")

    async def _socks5_connect(self, host, port):
        r, w = await asyncio.wait_for(
            asyncio.open_connection(self.remote_host, self.remote_port), timeout=15)
        try:
            w.write(b"\x05\x01\x00")          # version5, 1 method: no-auth
            await w.drain()
            rep = await asyncio.wait_for(r.readexactly(2), timeout=10)
            if rep[1] != 0x00:
                raise ConnectionError("socks5 认证方式不被支持")
            if ip_is_v4(host):
                atyp, addr = 0x01, socket.inet_aton(host)
            elif ip_is_v6(host):
                atyp, addr = 0x04, socket.inet_pton(socket.AF_INET6, host)
            else:
                b = host.encode("utf-8")
                if len(b) > 255:
                    raise ValueError("域名过长")
                atyp, addr = 0x03, bytes([len(b)]) + b
            req = bytes([0x05, 0x01, 0x00, atyp]) + addr + \
                port.to_bytes(2, "big")
            w.write(req)
            await w.drain()
            head = await asyncio.wait_for(r.readexactly(4), timeout=10)
            if head[1] != 0x00:
                raise ConnectionError("socks5 连接失败 code=%d" % head[1])
            # 跳过 BND.ADDR/BND.PORT
            atype = head[3]
            if atype == 0x01:
                await r.readexactly(4 + 2)
            elif atype == 0x04:
                await r.readexactly(16 + 2)
            else:
                ln = (await r.readexactly(1))[0]
                await r.readexactly(ln + 2)
            return r, w
        except Exception:
            w.close()
            raise


def ip_is_v4(s):
    try:
        socket.inet_aton(s)
        return True
    except Exception:
        return False


def ip_is_v6(s):
    try:
        socket.inet_pton(socket.AF_INET6, s)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 代理请求处理
# --------------------------------------------------------------------------

async def pump(src, dst):
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def relay(reader, writer, rtarget, wtarget, first=None):
    """双向搬运; first 为已从客户端读到、需要先写向目标的字节。"""
    if first:
        wtarget.write(first)
        await wtarget.drain()
    t1 = asyncio.create_task(pump(reader, wtarget))
    t2 = asyncio.create_task(pump(rtarget, writer))
    done, pending = await asyncio.wait(
        {t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        try:
            await t
        except Exception:
            pass
    for sock in (wtarget, writer):
        try:
            sock.close()
        except Exception:
            pass


async def handle_proxy_client(reader, writer, state):
    """入口: 处理一个代理客户端连接。"""
    settings = state.load()
    peer = writer.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    except asyncio.IncompleteReadError as e:
        if e.partial:
            head = e.partial
        else:
            writer.close()
            return
    except (asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
        writer.close()
        return

    try:
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ")
        if len(parts) < 3:
            writer.close()
            return
        method = parts[0].decode("latin-1").upper()
        target = parts[1].decode("latin-1")
        ver = parts[2].decode("latin-1")
    except Exception:
        writer.close()
        return

    # 是否允许代理
    if not settings.get("enabled"):
        _http_status(writer, 403, "Forbidden",
                     b"proxy disabled: enable it in the accelerator panel")
        await writer.drain()
        writer.close()
        return

    host, port = None, None
    if method == "CONNECT":
        # CONNECT host:port
        try:
            host, port = split_host_port(target, 443)
        except Exception:
            writer.close()
            return
        if not acl_allowed(settings, host):
            _http_status(writer, 403, "Forbidden",
                         ("%s not in whitelist" % host).encode())
            await writer.drain()
            writer.close()
            return
        try:
            r_up, w_up = await _upstream_connect(settings, host, port)
        except Exception as e:
            _http_status(writer, 502, "Bad Gateway",
                         ("upstream connect failed: %s" % e).encode())
            await writer.drain()
            writer.close()
            return
        _http_status(writer, 200, "Connection Established", b"")
        await writer.drain()
        await relay(reader, writer, r_up, w_up)
    else:
        # 绝对 URI 的普通 HTTP 请求(http:// 形式)
        try:
            parsed = urllib.parse.urlsplit(target)
            if not parsed.hostname:
                writer.close()
                return
            host, port = parsed.hostname, parsed.port or 80
        except Exception:
            writer.close()
            return
        if not acl_allowed(settings, host):
            _http_status(writer, 403, "Forbidden",
                         ("%s not in whitelist" % host).encode())
            await writer.drain()
            writer.close()
            return
        try:
            r_up, w_up = await _upstream_connect(settings, host, port)
        except Exception as e:
            _http_status(writer, 502, "Bad Gateway",
                         ("upstream connect failed: %s" % e).encode())
            await writer.drain()
            writer.close()
            return

        settings_mode = settings.get("mode", "http")
        if settings_mode == "http":
            # 上游就是 http 代理: 绝对 URI 原样透传即可
            first = head
        else:
            # 直连/socks5 直连目标: 必须改写为 origin-form 请求行
            first = _rewrite_origin_form(method, head, parsed, host)
        await relay(reader, writer, r_up, w_up, first=first)


def _rewrite_origin_form(method, raw_head, parsed, host):
    """把 'GET http://host/path HTTP/1.1' 改写为 origin-form 请求。"""
    lines = raw_head.split(b"\r\n")
    new_lines = []
    keep_conn_close = True
    for idx, line in enumerate(lines):
        if idx == 0:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            new_lines.append(("%s %s HTTP/1.1" % (method, path)).encode("latin-1"))
            continue
        low = line[:1].lower() + line[1:]  # 不改变原字节, 仅用于判断
        name = line.split(b":", 1)[0].lower().strip()
        if name == b"proxy-connection":
            continue
        if name == b"connection":
            continue
        if name == b"host" and (not parsed.hostname or host):
            continue  # 下面统一补 Host
        new_lines.append(line)
    new_lines.append(b"Host: " + host.encode("idna") + b":" +
                     str(parsed.port or 80).encode())
    new_lines.append(b"Connection: close")
    new_lines.append(b"")
    new_lines.append(b"")
    return b"\r\n".join(new_lines)


async def _upstream_connect(settings, host, port):
    uc = UpstreamConn(settings)
    return await uc.connect_target(host, port)


# --------------------------------------------------------------------------
# 面板 REST API + 静态文件
# --------------------------------------------------------------------------

MIRROR_TEST_CACHE = {}
MIRROR_CACHE_TTL = 60


def normalize_ingress_path(path, headers):
    """新版 Supervisor(Apps) 的 Ingress 会把 /api/hassio_ingress/<token>/...
    前缀转发进容器; 剥离该前缀后再路由, 同时兼容容器内直连(无前缀)。

    优先用 Supervisor 注入的 X-Ingress-Path 请求头, 否则用路径正则兜底。
    """
    for k, v in headers:
        if k.lower() == "x-ingress-path" and v and path.startswith(v):
            rest = path[len(v):]
            return rest or "/"
    m = re.match(r"^/api/hassio_ingress/[^/]+(/.*)?$", path)
    if m:
        return m.group(1) or "/"
    return path


class WebHandler:
    def __init__(self, state):
        self.state = state

    async def route(self, reader, writer):
        try:
            req = await asyncio.wait_for(
                _read_request_head(reader), timeout=10)
        except Exception:
            writer.close()
            return
        method = req["method"]
        path = normalize_ingress_path(
            urllib.parse.urlsplit(req["target"]).path, req["headers"])
        headers = req["headers"]

        try:
            if path in ("/", "/index.html") and method == "GET":
                return await self._static(writer, "index.html")
            if path.startswith("/static/"):
                return await self._static(writer, path[len("/static/"):])
            if path == "/api/state" and method == "GET":
                return await self._json(writer, 200, self._public_state())
            if path == "/api/state" and method == "POST":
                body = await self._read_body(reader, headers)
                cur = self.state.update(json.loads(body or "{}"))
                return await self._json(writer, 200, self._public_state(cur))
            if path == "/api/toggle" and method == "POST":
                body = json.loads(await self._read_body(reader, headers) or "{}")
                cur = self.state.load()
                if "enabled" in body:
                    cur["enabled"] = bool(body["enabled"])
                if "hacs_patch_enabled" in body:
                    cur["hacs_patch_enabled"] = bool(body["hacs_patch_enabled"])
                self.state.save(cur)
                return await self._json(writer, 200, self._public_state(cur))
            if path == "/api/probe" and method == "POST":
                body = json.loads(await self._read_body(reader, headers) or "{}")
                result = await probe_targets(body)
                return await self._json(writer, 200, result)
            if path == "/api/mirrors" and method == "GET":
                return await self._json(writer, 200,
                                        await check_mirrors(self.state.load()))
            if path == "/api/version" and method == "GET":
                return await self._json(writer, 200, {"version": APP_VERSION})
            if path.startswith("/api/hacs/"):
                return await self._hacs(writer, path, method, reader, headers)
            if path == "/api/restart-ha" and method == "POST":
                return await self._restart_ha(writer)
            return await self._json(writer, 404, {"error": "not found"})
        except Exception as e:
            try:
                return await self._json(writer, 500, {"error": str(e)})
            except Exception:
                writer.close()

    def _public_state(self, cur=None):
        cur = cur or self.state.load()
        pub = json.loads(json.dumps(cur))
        pub["upstream_pass"] = ""
        return pub

    async def _read_body(self, reader, headers, limit=1 << 20):
        length = 0
        for k, v in headers:
            if k.lower() == "content-length":
                try:
                    length = int(v)
                except ValueError:
                    length = 0
        if length <= 0 or length > limit:
            return b""
        return await asyncio.wait_for(reader.readexactly(length), timeout=10)

    async def _restart_ha(self, writer):
        """HACS 补丁应用后重启 Home Assistant core(Syn Supervisor API)。"""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return await self._json(writer, 200, {
                "ok": False,
                "error": "未检测到 Supervisor 令牌(仅 add-on 环境可用); 请手动重启 HA"})
        loop = asyncio.get_event_loop()

        def _call():
            import urllib.request
            req = urllib.request.Request(
                "http://supervisor/core/restart", data=b"{}", method="POST",
                headers={"Authorization": "Bearer %s" % token,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status

        try:
            status = await loop.run_in_executor(None, _call)
            return await self._json(writer, 200, {"ok": status == 200,
                                                  "status": status})
        except Exception as e:
            return await self._json(writer, 200, {"ok": False,
                                                  "error": str(e)})

    async def _hacs(self, writer, path, method, reader, headers):
        try:
            import hacs_patch  # 见 hacs_patch.py: 状态/打补丁/回滚
        except Exception as e:
            return await self._json(writer, 501, {
                "error": "hacs_patch 模块不可用: %s" % e})
        try:
            if path == "/api/hacs/status" and method == "GET":
                return await self._json(writer, 200, hacs_patch.status())
            if path == "/api/hacs/apply" and method == "POST":
                body = json.loads(await self._read_body(reader, headers) or "{}")
                enabled = bool(body.get("enabled", True))
                res = hacs_patch.apply(enabled)
                return await self._json(writer, 200, {"ok": True, **res})
            return await self._json(writer, 404, {"error": "no such action"})
        except Exception as e:
            return await self._json(writer, 500, {"ok": False, "error": str(e)})

    async def _static(self, writer, rel):
        if ".." in rel or rel.startswith("/"):
            return await self._json(writer, 400, {"error": "bad path"})
        path = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(path):
            return await self._json(writer, 404, {"error": "no such file"})
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        _http_status(writer, 200, "OK", data,
                     ctype.encode("utf-8") if False else
                     ("%s; charset=utf-8" % ctype if ctype.startswith("text/") or
                      ctype in ("application/javascript", "application/json")
                      else ctype).encode("utf-8"))
        await writer.drain()
        writer.close()

    async def _json(self, writer, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        _http_status(writer, code,
                     "OK" if code < 400 else "Error", body,
                     b"application/json; charset=utf-8")
        await writer.drain()
        writer.close()


async def _read_request_head(reader):
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.split(b"\r\n")
    parts = lines[0].split(b" ")
    headers = []
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers.append((k.decode("latin-1").strip(),
                            v.decode("latin-1").strip()))
    return {"method": parts[0].decode("latin-1").upper(),
            "target": parts[1].decode("latin-1"), "headers": headers}


async def probe_targets(body):
    """探测目标: mode=http/socks5 检查上游代理; kind=https 检查 url 的 TLS 可达性。"""
    results = {}
    targets = body.get("targets") or []
    for item in targets:
        try:
            if item.get("kind") == "https" or item.get("mode") == "https":
                url = item.get("url") or item.get("addr")
                if not url:
                    raise ValueError("missing url")
                ok, ms, info = await _ping_https(url)
                results[item.get("id", url)] = {
                    "ok": ok, "ms": ms, "url": url, "info": info}
                continue
            host, port = split_host_port(item.get("addr", ""), 7890)
            mode = item.get("mode", "http")
            start = time.monotonic()
            if mode == "socks5":
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=5)
                w.write(b"\x05\x01\x00")
                await w.drain()
                rep = await asyncio.wait_for(r.readexactly(2), timeout=5)
                ok = rep[0] == 5
                w.close()
            elif mode == "http":
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=5)
                w.write(b"GET http://example.com/ HTTP/1.1\r\n"
                        b"Host: example.com\r\nConnection: close\r\n\r\n")
                data = await asyncio.wait_for(r.read(64), timeout=6)
                ok = data.startswith(b"HTTP/")
                w.close()
            else:
                ok = False
            ms = int((time.monotonic() - start) * 1000)
            results[item.get("id", item.get("addr"))] = {
                "ok": ok, "ms": ms, "addr": item.get("addr")}
        except Exception as e:
            results[item.get("id", item.get("addr"))] = {
                "ok": False, "ms": -1, "addr": item.get("addr"),
                "error": str(e)}
    return results


async def _ping_https(url, timeout=6):
    """对镜像做 HTTPS GET(仅首包), 返回 (ok, ms, status)。"""
    parsed = urllib.parse.urlsplit(url)
    host, port = parsed.hostname, parsed.port or 443
    start = time.monotonic()
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=True), timeout=timeout)
        req = ("GET /v2/ HTTP/1.1\r\nHost: %s\r\nUser-Agent: ha-proxy-acc/1.0"
               "\r\nConnection: close\r\n\r\n" % host).encode()
        w.write(req)
        await w.drain()
        data = await asyncio.wait_for(r.read(256), timeout=timeout)
        w.close()
        ms = int((time.monotonic() - start) * 1000)
        line = data.split(b"\r\n", 1)[0] if data else b""
        return True, ms, line.decode("latin-1", "replace")
    except Exception as e:
        return False, -1, str(e)


async def check_mirrors(settings):
    """探活配置里的 Docker Hub / GHCR 镜像(带 60s 缓存)。"""
    now = time.monotonic()
    out = {"docker_hub": [], "ghcr": [], "ts": now}
    for kind, key in (("docker_hub", "docker_hub_mirrors"),
                      ("ghcr", "ghcr_mirrors")):
        for url in settings.get(key, []):
            cache = MIRROR_TEST_CACHE.get(url)
            if cache and now - cache["ts"] < MIRROR_CACHE_TTL:
                item = dict(cache)
            else:
                ok, ms, info = await _ping_https(url)
                item = {"url": url, "ok": ok, "ms": ms, "info": info,
                        "ts": now}
                MIRROR_TEST_CACHE[url] = item
            out[kind].append(item)
    return out


APP_VERSION = "0.3.2"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-port", type=int, default=8899)
    ap.add_argument("--web-port", type=int, default=8099)
    args = ap.parse_args()

    state = State(os.environ.get("STATE_FILE", "/data/settings.json"))
    if not os.path.exists(state.path):
        state.save(default_settings())
        print("[init] wrote default settings to", state.path)

    handler = WebHandler(state)

    async def proxy_cb(reader, writer):
        await handle_proxy_client(reader, writer, state)

    async def web_cb(reader, writer):
        await handler.route(reader, writer)

    server = await asyncio.start_server(proxy_cb, "0.0.0.0", args.proxy_port)
    websrv = await asyncio.start_server(web_cb, "0.0.0.0", args.web_port)
    print("[up] proxy=%s web=%s state=%s" % (args.proxy_port, args.web_port,
                                             state.path), flush=True)
    await asyncio.gather(server.serve_forever(), websrv.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
