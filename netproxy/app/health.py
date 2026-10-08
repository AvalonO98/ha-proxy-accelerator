"""看门狗：代理方式 B/C 开启时，持续确认"通过代理真的能到 registry"，
超时未恢复则自动回滚并关闭总开关（防止插件把自己或 HA 的更新通道锁死）。
"""
from __future__ import annotations

import threading
import time

from . import jobs, mihomo, modes, settings
from .logbus import log

HEALTH_URL = "https://ghcr.io/v2/"
INTERVAL = 20


class Watchdog(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="watchdog", daemon=True)
        self._fails = 0
        self._first_fail = None

    def _healthy(self) -> tuple:
        if not mihomo.alive():
            return False, "内核进程不在运行"
        if not mihomo._port_open("127.0.0.1", mihomo.PROXY_PORT):
            return False, f"{mihomo.PROXY_PORT} 端口未监听"
        r = mihomo.http_fetch(HEALTH_URL, via_proxy=True, timeout=10)
        if not r.get("ok"):
            return False, f"经代理访问 {HEALTH_URL} 失败：{r.get('error')}"
        return True, f"ok (HTTP {r.get('status')}, {r.get('ms')}ms)"

    def run(self) -> None:
        while True:
            time.sleep(INTERVAL)
            try:
                self._tick()
            except Exception as e:  # pragma: no cover
                log(f"看门狗异常：{type(e).__name__}: {e}", "error", "watchdog")

    def _tick(self) -> None:
        cfg = settings.load_config()
        st = settings.load_state()
        applied = st.get("applied") or {}

        if not cfg.get("enabled") or cfg.get("mode") not in ("upstream_proxy", "wireguard"):
            self._fails = 0
            self._first_fail = None
            return
        if not cfg.get("auto_rollback"):
            return
        if applied.get("pending_verify"):
            return  # 正在等 dockerd 重启后的校验，别插手
        if jobs.busy():
            return

        ok, detail = self._healthy()
        if ok:
            if self._fails:
                log(f"代理已恢复健康（{detail}）", "notice", "watchdog")
            self._fails = 0
            self._first_fail = None
            return

        self._fails += 1
        now = time.time()
        if self._first_fail is None:
            self._first_fail = now
        duration = now - self._first_fail
        log(f"代理健康检查失败（第 {self._fails} 次，已持续 {int(duration)}s）：{detail}",
            "warning", "watchdog")

        if duration >= int(cfg.get("health_check_seconds", 300)):
            log(f"代理持续不可用 {int(duration)}s，超过阈值 → 自动回滚并关闭总开关", "error", "watchdog")
            self._fails = 0
            self._first_fail = None
            cfg["enabled"] = False
            settings.save_config(cfg)
            try:
                mihomo.stop()
            except Exception:
                pass
            jobs.submit("auto-rollback", lambda job: modes.apply(settings.load_config()))
