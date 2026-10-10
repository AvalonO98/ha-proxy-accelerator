"""调用 Supervisor 自身 API（关闭自己的保护模式 / 重启自己 / 查询自身信息）。

背景（实测结论）：
  Supervisor 的「保护模式」默认开启，且**无法由插件的 config.yaml 声明**
  （`ATTR_PROTECTED` 只存在于 SCHEMA_APP_USER）。
  而 `supervisor/docker/app.py` 中：

      def pid_mode(self):
          return "host" if not self.app.protected and self.app.host_pid else None
      if not self.app.protected and self.app.access_docker_api: ...

  保护模式一旦开启，host_pid 与 docker_api 都不会生效，本插件彻底失效。

  好在 `supervisor/api/__init__.py` 里 `/addons/{app}/security` 注册在 **V1 API 组**
  （`hassio_role: default` 即可访问），且 `supervisor/api/apps.py::security` 用的是
  `get_app_for_request`、没有像 `options` 那样禁止 `self` —— 所以插件可以关闭
  **自己**的保护模式，再重启自己让新参数生效。

⚠️ 另一个实测踩到的坑：本插件声明了 `host_network: true`，容器在**宿主网络命名空间**里，
   `http://supervisor` 这个 hassio 网络内的 DNS 名**解析不到**（表现为所有 Supervisor API
   调用都失败、`/api/diag` 的 `supervisor_self` 为空）。因此这里按候选列表逐个探测并缓存。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional, Tuple

#: 候选地址（按优先级）。host_network 场景下 `supervisor` 解析不了，所以 172.30.32.2 排前面。
BASE_CANDIDATES = (
    "http://172.30.32.2",        # Supervisor 在 hassio 网桥上的地址（host_network: true 必需）
    "http://supervisor",         # hassio 网络内的 DNS 名（host_network: false 时可用）
    "http://hassio-supervisor",  # 兼容旧命名
)

_base: Optional[str] = None
_tried: list = []


def _token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "")


def available() -> bool:
    return bool(_token())


def _candidates() -> list:
    env = (os.environ.get("NETPROXY_SUPERVISOR_API") or "").strip().rstrip("/")
    out, seen = [], set()
    for base in ([env] if env else []) + list(BASE_CANDIDATES):
        if base and base not in seen:
            seen.add(base)
            out.append(base)
    return out


def _log(msg: str, level: str = "info") -> None:
    try:
        from .logbus import log
        log(msg, level, "supervisor")
    except Exception:
        pass


def _probe(base: str, timeout: float = 6.0) -> Tuple[int, str]:
    """探测地址是否"能连上"。返回 (HTTP 状态码, 说明)；-1 表示连不上。
    任何 HTTP 响应（含 401/403/404）都算网络可达。"""
    req = urllib.request.Request(base + "/", method="GET",
                                headers={"Authorization": f"Bearer {_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, "ok"
    except urllib.error.HTTPError as e:
        return e.code, f"http {e.code}"
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def resolve_base(force: bool = False) -> Optional[str]:
    """解析并缓存 Supervisor API 的可用基地址。"""
    global _base, _tried
    if _base and not force:
        return _base
    _tried = []
    for base in _candidates():
        code, detail = _probe(base)
        _tried.append(f"{base} -> {detail}")
        if code > 0:
            _base = base
            _log(f"Supervisor API 使用 {base}", "notice")
            return _base
    _base = None
    _log("无法连接 Supervisor API，已尝试：" + " ｜ ".join(_tried), "warning")
    return None


def _request(method: str, path: str, payload: Optional[dict] = None,
             timeout: float = 45.0) -> Tuple[int, str]:
    tok = _token()
    if not tok:
        return -1, "没有 SUPERVISOR_TOKEN（插件可能不在 Supervisor 环境下运行）"
    base = resolve_base()
    if not base:
        return -1, "无法连接 Supervisor API（已尝试：" + " ｜ ".join(_tried or []) + "）"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # 不要截断得太短：/addons/self/info 的 JSON 有好几 KB，截断会导致 json.loads 失败
            return r.status, r.read().decode("utf-8", "replace")[:65536]
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def self_info() -> dict:
    code, body = _request("GET", "/addons/self/info")
    if code != 200:
        return {"_error": f"HTTP {code}: {body[:300]}"}
    try:
        return json.loads(body).get("data", {})
    except Exception as e:
        return {"_error": f"解析失败：{type(e).__name__}: {e}（响应前 200 字符：{body[:200]}）"}


def disable_protection() -> Tuple[bool, str]:
    """POST /addons/self/security {"protected": false}"""
    code, body = _request("POST", "/addons/self/security", {"protected": False})
    if code in (200, 201):
        return True, body or "ok"
    return False, f"HTTP {code}: {body}"


def restart_self() -> Tuple[bool, str]:
    """请求 Supervisor 重启本插件（会中断当前 HTTP 请求，属预期）。"""
    code, body = _request("POST", "/addons/self/restart", None, timeout=20.0)
    if code in (200, 201):
        return True, body or "ok"
    return False, f"HTTP {code}: {body}"


def diagnostics() -> dict:
    """给 /api/diag 用：基地址、探测过程、自身信息（含错误原因）。"""
    info = self_info()
    return {
        "base": _base or resolve_base(),
        "tried": _tried,
        "token_present": available(),
        "self": info,
        "protected": info.get("protected"),
    }
