"""调用 Supervisor 自身 API（关闭自己的保护模式 / 重启自己）。

背景（实测结论）：
  Supervisor 的「保护模式」默认开启，且**无法由插件的 config.yaml 声明**
  （`ATTR_PROTECTED` 只存在于 SCHEMA_APP_USER，插件配置里的同名键会被忽略）。
  而 `supervisor/docker/app.py` 中：

      def pid_mode(self):
          return "host" if not self.app.protected and self.app.host_pid else None
      if not self.app.protected and self.app.access_docker_api: ...

  也就是说保护模式一旦开启，host_pid 与 docker_api 都不会生效，本插件彻底失效。

  好消息是 `supervisor/api/apps.py::security` 用的是 `get_app_for_request`，
  且没有像 `options` 那样禁止 `self` —— 所以插件可以关闭**自己**的保护模式，
  然后重启自己让新的容器参数生效（保护模式只在容器创建时体现）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional, Tuple

BASE = "http://supervisor"


def _token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "")


def available() -> bool:
    return bool(_token())


def _request(method: str, path: str, payload: Optional[dict] = None,
             timeout: float = 45.0) -> Tuple[int, str]:
    tok = _token()
    if not tok:
        return -1, "没有 SUPERVISOR_TOKEN（插件可能不在 Supervisor 环境下运行）"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def self_info() -> dict:
    code, body = _request("GET", "/addons/self/info")
    if code != 200:
        return {"_error": f"HTTP {code}: {body}"}
    try:
        return json.loads(body).get("data", {})
    except Exception:
        return {"_error": body}


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
