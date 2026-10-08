"""Docker Engine API 客户端（走 /var/run/docker.sock）。

为什么不用 `docker` 命令行：
  * HAOS 宿主上不一定有可用的 docker CLI，也不该依赖它；
  * `docker info` 反映的是 dockerd 的**实时**配置，这正是本插件要的"真实生效"
    校验（RegistryConfig.Mirrors / HttpProxy / HttpsProxy / NoProxy）。

`docker_api: true` 由 Supervisor 挂入 socket（通常只读）。只读时 GET 可用，
POST（拉镜像实测）会被拒绝，此时降级为只读模式并如实上报。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Optional, Tuple

SOCKET_PATHS = ("/var/run/docker.sock", "/run/docker.sock")

_api_version: Optional[str] = None


def socket_path() -> Optional[str]:
    for p in SOCKET_PATHS:
        if os.path.exists(p):
            return p
    return None


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):  # noqa: D401
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _request(method: str, path: str, body: bytes = b"", timeout: float = 15.0,
             versioned: bool = True, raw: bool = False) -> Tuple[int, object]:
    sp = socket_path()
    if not sp:
        return -1, "docker socket not found (需要 docker_api: true)"
    prefix = ""
    if versioned:
        v = _version_prefix()
        if v is None:
            return -1, "cannot determine Docker API version"
        prefix = v
    conn = _UnixConnection(sp, timeout=timeout)
    status = -1
    data: bytes = b""
    try:
        headers = {"Host": "docker", "Content-Type": "application/json"}
        conn.request(method, prefix + path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        data = resp.read()
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if raw:
        return status, data
    try:
        return status, json.loads(data.decode("utf-8", "replace") or "{}")
    except Exception:
        return status, data.decode("utf-8", "replace")


def _version_prefix() -> Optional[str]:
    global _api_version
    if _api_version is None:
        status, data = _request("GET", "/version", versioned=False, timeout=8.0)
        if status == 200 and isinstance(data, dict):
            _api_version = "/v" + str(data.get("ApiVersion", "1.41"))
        else:
            return None
    return _api_version


def info() -> dict:
    """dockerd 实时信息；失败时返回 {"_error": ...}。"""
    status, data = _request("GET", "/info", timeout=15.0)
    if status != 200 or not isinstance(data, dict):
        return {"_error": f"HTTP {status}: {data}"}
    return data


def version() -> dict:
    status, data = _request("GET", "/version", versioned=False, timeout=8.0)
    if status != 200 or not isinstance(data, dict):
        return {"_error": f"HTTP {status}: {data}"}
    return data


def summarize() -> dict:
    """面板用的精简状态：镜像源与代理是否真的生效。"""
    i = info()
    if "_error" in i:
        return {"available": False, "error": i["_error"]}

    reg = i.get("RegistryConfig") or {}
    v = version()
    return {
        "available": True,
        "server_version": i.get("ServerVersion"),
        "api_version": v.get("ApiVersion"),
        "storage_driver": i.get("Driver"),
        "registry_mirrors": reg.get("Mirrors") or [],
        "insecure_registries": i.get("InsecureRegistryCIDRs") or reg.get("InsecureRegistryCIDRs") or [],
        "http_proxy": i.get("HttpProxy") or "",
        "https_proxy": i.get("HttpsProxy") or "",
        "no_proxy": i.get("NoProxy") or "",
        "live_restore": i.get("LiveRestoreEnabled"),
        "containers": i.get("Containers"),
        "images": i.get("Images"),
        "raw_registry_config": reg,
    }


def pull_test(image: str, timeout: float = 240.0, progress=None) -> Tuple[bool, str]:
    """真实拉取镜像，端到端验证代理/镜像源是否对 dockerd 生效。

    只读 socket 会返回 403/405，此时如实报告"无写权限，无法实测"。
    """
    sp = socket_path()
    if not sp:
        return False, "docker socket not found"
    from urllib.parse import quote

    v = _version_prefix() or ""
    path = f"{v}/images/create?fromImage={quote(image, safe='')}"
    conn = _UnixConnection(sp, timeout=timeout)
    try:
        conn.request("POST", path, body=b"", headers={"Host": "docker", "Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status not in (200, 201):
            body = resp.read().decode("utf-8", "replace")[:400]
            return False, f"HTTP {resp.status}: {body}"
        last = ""
        while True:
            chunk = resp.readline()
            if not chunk:
                break
            try:
                ev = json.loads(chunk.decode("utf-8", "replace"))
            except Exception:
                continue
            if isinstance(ev, dict):
                if ev.get("error"):
                    return False, str(ev["error"])[:400]
                msg = ev.get("status") or ev.get("stream") or ""
                if msg:
                    last = msg.strip()
                    if progress:
                        progress(last)
        return True, last or "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ping() -> Tuple[bool, str]:
    status, data = _request("GET", "/_ping", versioned=False, timeout=8.0, raw=True)
    if status == 200:
        return True, "ok"
    return False, f"HTTP {status}: {data}"
