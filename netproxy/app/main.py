"""Net Proxy add-on 的 HTTP 服务（Python 标准库，无第三方依赖）。

* Ingress 面板：Supervisor 反代到容器 8099，前端用相对路径，天然兼容
  /api/hassio_ingress/<token>/ 前缀；
* 鉴权：带 X-Ingress-Path 头（来自 Supervisor）或来自 docker 内网/回环的请求
  直接放行；若用户手动把端口发布到局域网，则要求一次性令牌（见 /data 里的
  api_token，启动时会打到日志里）；
* 所有会改宿主状态的操作用后台任务，避免 ingress HTTP 超时。
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import (daemonconf, dockerapi, health, hostops, jobs, mihomo, modes,
               settings, supervisor_api, wg)
from .logbus import LOG, log

APP_DIR = os.environ.get("NETPROXY_APP", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(APP_DIR, "static")
PORT = int(os.environ.get("NETPROXY_PORT", "8099"))


def _json_default(o):
    return str(o)


class Handler(BaseHTTPRequestHandler):
    server_version = "netproxy/0.1.0"
    protocol_version = "HTTP/1.1"

    # ----------------------------------------------------------------- #
    # 基础设施
    # ----------------------------------------------------------------- #
    def log_message(self, fmt, *args):  # 收敛访问日志到环形缓冲
        log(fmt % args, "debug", "http")

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _authorized(self) -> bool:
        path = urlparse(self.path).path
        if path.startswith("/api/health"):
            return True
        if self.headers.get("X-Ingress-Path"):
            return True
        ip = self._client_ip()
        if ip.startswith("127.") or ip.startswith("172.30.") or ip in ("::1", "localhost"):
            return True
        q = parse_qs(urlparse(self.path).query)
        tok = self.headers.get("X-Netproxy-Token") or (q.get("token") or [""])[0]
        return bool(tok) and hmac.compare_digest(tok, settings.api_token())

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8"))

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {"value": data}
        except Exception:
            return {}

    def _norm_path(self) -> str:
        p = urlparse(self.path).path
        # 幂等：即使 Supervisor 没剥掉 ingress 前缀也能工作
        marker = "/api/hassio_ingress/"
        if marker in p:
            rest = p.split(marker, 1)[1]
            parts = rest.split("/", 1)
            p = "/" + (parts[1] if len(parts) > 1 else "")
        return p

    # ----------------------------------------------------------------- #
    # 路由
    # ----------------------------------------------------------------- #
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("GET")

    def _dispatch(self, method: str):
        path = self._norm_path()
        try:
            if not self._authorized():
                return self._err(401, "未授权：请通过 HA 侧边栏(ingress)访问，或带上 token")
            if path.startswith("/api/"):
                return self._api(method, path)
            return self._static(path)
        except Exception as e:
            log(f"请求处理异常 {method} {path}: {type(e).__name__}: {e}", "error", "http")
            log(traceback.format_exc(limit=6), "debug", "http")
            return self._err(500, f"{type(e).__name__}: {e}")

    def _api(self, method: str, path: str):
        q = parse_qs(urlparse(self.path).query)
        cfg = settings.load_config()

        if path == "/api/health":
            return self._json({"ok": True, "ts": round(time.time(), 3)})

        if path == "/api/status" and method == "GET":
            return self._json(modes.status(cfg))

        if path == "/api/config" and method == "GET":
            return self._json({"config": cfg, "source": cfg.get("_source"),
                               "token_hint": "直连端口访问需要在 URL 上带 ?token="})

        if path == "/api/config" and method == "POST":
            body = self._body()
            incoming = body.get("config") or body
            if not isinstance(incoming, dict):
                return self._err(400, "config 必须是对象")
            merged = dict(cfg)
            merged.pop("_source", None)
            merged.update(incoming)
            saved = settings.save_config(merged)
            return self._json({"config": saved, "needs_apply": True})

        if path == "/api/toggle" and method == "POST":
            body = self._body()
            want = bool(body.get("enabled"))
            prev = bool(cfg.get("enabled"))
            cfg["enabled"] = want
            settings.save_config(cfg)
            job = _submit_apply(prev_enabled=prev)
            return self._json({"enabled": want, "job": job})

        if path == "/api/apply" and method == "POST":
            body = self._body()
            if isinstance(body.get("config"), dict):
                merged = dict(cfg)
                merged.pop("_source", None)
                merged.update(body["config"])
                cfg = settings.save_config(merged)
            if body.get("enabled") is not None:
                cfg["enabled"] = bool(body["enabled"])
                settings.save_config(cfg)
            return self._json({"job": _submit_apply()})

        if path == "/api/rollback" and method == "POST":
            body = self._body()
            bid = body.get("backup_id")
            return self._json({"job": jobs.submit("rollback", lambda job: _do_rollback(bid, job))})

        if path == "/api/kernel/install" and method == "POST":
            body = self._body()
            url = (body.get("url") or cfg.get("kernel_url") or "").strip()
            return self._json({"job": jobs.submit("kernel-install",
                                                  lambda job: _do_kernel_install(url, job))})

        if path == "/api/kernel/restart" and method == "POST":
            return self._json({"job": jobs.submit("kernel-restart", lambda job: _do_kernel_restart(job))})

        if path == "/api/test" and method == "POST":
            return self._json({"job": jobs.submit("connectivity",
                                                  lambda job: modes.connectivity_test(settings.load_config()),
                                                  mutating=False)})

        if path == "/api/pull-test" and method == "POST":
            body = self._body()
            image = (body.get("image") or "hello-world:latest").strip()
            return self._json({"job": jobs.submit("pull-test",
                                                  lambda job: _do_pull_test(image, job), mutating=False)})

        if path == "/api/logs" and method == "GET":
            since = int((q.get("since") or ["0"])[0] or 0)
            recs = LOG.since(since, limit=int((q.get("limit") or ["400"])[0] or 400))
            return self._json({"logs": recs, "last_seq": recs[-1]["seq"] if recs else since})

        if path == "/api/jobs" and method == "GET":
            cur = jobs.current()
            return self._json({"current": cur, "recent": jobs.list_jobs(10)})

        if path == "/api/job" and method == "GET":
            jid = (q.get("id") or [""])[0]
            j = jobs.get(jid) if jid else jobs.current()
            if not j:
                return self._err(404, "没有这个任务")
            j["logs"] = jobs.job_log(j)
            return self._json({"job": j})

        if path == "/api/backups" and method == "GET":
            return self._json({"backups": daemonconf.list_backups(50)})

        if path == "/api/diag" and method == "GET":
            return self._json(_diag(cfg))

        if path == "/api/self-heal" and method == "POST":
            return self._json({"job": jobs.submit("self-heal", _do_self_heal)})

        if path == "/api/wg/parse" and method == "POST":
            body = self._body()
            return self._json({"result": wg.summarize(body.get("config") or "")})

        return self._err(404, f"未知接口 {path}")

    def _static(self, path: str):
        if path in ("/", "", "/index.html"):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            full = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(full):
                return self._err(404, "静态资源缺失")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


# --------------------------------------------------------------------------- #
# 任务体
# --------------------------------------------------------------------------- #
def _submit_apply(prev_enabled=None) -> dict:
    def _run(job):
        cfg = settings.load_config()
        try:
            return modes.apply(cfg, job)
        except Exception:
            if prev_enabled is not None:
                cfg["enabled"] = bool(prev_enabled)
                settings.save_config(cfg)
                log(f"应用失败，总开关已回滚为 {prev_enabled}", "warning", "apply")
            raise

    return jobs.submit("apply", _run)


def _do_rollback(backup_id, job) -> dict:
    """把 daemon.json 恢复到某个备份（或不带参数时恢复到本次应用之前），并关掉总开关。"""
    pre = daemonconf.read_current()   # 回滚前的现场，用作重启脚本的兜底备份
    if backup_id:
        rec = daemonconf.load_backup(backup_id)
        if not rec:
            raise RuntimeError(f"找不到备份 {backup_id}")
        ok, msg = daemonconf.restore_raw(rec.get("raw"), bool(rec.get("existed")))
    else:
        st = settings.load_state()
        applied = st.get("applied") or {}
        if not applied:
            raise RuntimeError("没有可回滚的记录")
        ok, msg = daemonconf.restore_raw(applied.get("pre_raw"), bool(applied.get("pre_existed")))
    if not ok:
        raise RuntimeError(f"恢复失败：{msg}")

    cfg = settings.load_config()
    cfg["enabled"] = False
    settings.save_config(cfg)
    try:
        mihomo.stop()
    except Exception:
        pass

    # dockerd 当前若还在用代理，光改文件不生效，必须重启
    live = dockerapi.summarize()
    need_restart = bool(live.get("http_proxy") or live.get("https_proxy"))
    if need_restart:
        ok, msg = daemonconf.restart_dockerd_detached(
            int(cfg.get("apply_timeout_seconds", 180)),
            backup_raw=pre.get("raw"), backup_existed=bool(pre.get("existed")))
        if not ok:
            raise RuntimeError(f"回滚已改写文件，但重启 dockerd 失败：{msg}")
        settings.push_history("rollback", "手动回滚（已重启 dockerd）", {"backup": backup_id})
        return {"state": "restarting", "message": msg}
    daemonconf.sighup_dockerd()
    settings.push_history("rollback", "手动回滚（热加载）", {"backup": backup_id})
    return {"state": "applied", "message": "已恢复备份配置"}


def _do_kernel_install(url: str, job) -> dict:
    last = {"pct": -1}

    def progress(got, total):
        if total:
            pct = int(got * 100 / total)
            if pct != last["pct"] and pct % 10 == 0:
                last["pct"] = pct
                log(f"下载内核 {pct}%（{got // 1048576}MB / {total // 1048576}MB）", "info", "mihomo")
        else:
            log(f"下载内核 {got // 1048576}MB…", "info", "mihomo")

    ok, msg = mihomo.install(explicit_url=url, mirrors=cfg.get("github_mirrors"),
                             version=cfg.get("kernel_version"), progress=progress)
    if not ok:
        raise RuntimeError(msg)
    cfg = settings.load_config()
    if cfg.get("enabled") and cfg.get("mode") in ("upstream_proxy", "wireguard"):
        ok, msg2 = modes.ensure_kernel_running(cfg)
        if not ok:
            raise RuntimeError(f"内核已安装但启动失败：{msg2}")
    return {"state": "done", "version": msg, "message": "内核已安装"}


def _do_kernel_restart(job) -> dict:
    cfg = settings.load_config()
    ok, msg = modes.ensure_kernel_running(cfg)
    if not ok:
        raise RuntimeError(msg)
    return {"state": "done", "message": msg}


def _do_pull_test(image: str, job) -> dict:
    """端到端实测：让 dockerd 真的去拉一个镜像，验证镜像源/代理对它生效。"""
    log(f"实测拉取镜像：{image}", "notice", "pull-test")
    lines = []

    def progress(line):
        lines.append(line)
        if len(lines) % 5 == 0:
            log(f"  {line}", "info", "pull-test")

    ok, msg = dockerapi.pull_test(image, timeout=300, progress=progress)
    if not ok and ("403" in msg or "permission" in msg.lower() or "read-only" in msg.lower()):
        return {"state": "skipped", "message": f"docker socket 只读，无法实测拉取：{msg}"}
    if not ok:
        raise RuntimeError(f"拉取失败：{msg}")
    return {"state": "done", "message": f"拉取成功：{msg}", "tail": lines[-8:]}


def _do_self_heal(job) -> dict:
    """关闭自己的保护模式并重启插件。

    保护模式是 Supervisor 的用户级设置（插件无法在 config.yaml 里声明），默认开启，
    而它会让 host_pid / docker_api 全部失效 —— 于是必须由插件调用 Supervisor 的
    self 接口关掉它，再重启自己让新的容器参数生效。
    """
    if not supervisor_api.available():
        raise RuntimeError("没有 SUPERVISOR_TOKEN，无法调用 Supervisor API")
    info = supervisor_api.self_info()
    if info.get("protected") is False:
        log("保护模式已经是关闭状态；若仍无宿主访问，请检查 host_pid/privileged 是否生效，"
            "必要时卸载后重新安装插件。", "warning", "self-heal")
    ok, msg = supervisor_api.disable_protection()
    if not ok:
        raise RuntimeError("关闭保护模式失败：" + msg +
                           "（可在插件页面手动关闭「保护模式」后重启插件）")
    log("保护模式已关闭，正在重启插件以让新权限生效…", "notice", "self-heal")
    try:
        supervisor_api.restart_self()
    except Exception as e:
        log(f"重启请求异常（预期内，容器会被 Supervisor 重启）：{e}", "warning", "self-heal")
    return {"state": "restarting",
            "message": "保护模式已关闭，插件正在重启；约 10 秒后重新打开面板，宿主访问即可用。"}


def _diag(cfg: dict) -> dict:
    """给排查用的全量现场（不隐去密钥，但仅限内网/ingress 访问）。"""
    cur = daemonconf.read_current()
    return {
        "python": sys.version,
        "argv": sys.argv,
        "data_dir": str(settings.DATA_DIR),
        "namespace_mode": hostops.namespace_mode(),
        "capability": hostops.capability_report([daemonconf.DAEMON_JSON, mihomo._SHARE]),
        "host_daemon_json_raw": cur.get("raw"),
        "host_daemon_json_ok": cur.get("ok"),
        "host_daemon_json_error": cur.get("error"),
        "host_daemon_json_path": cur.get("path"),
        "host_run_probe": _probe(),
        "dockerd_pids": hostops.find_pids("dockerd"),
        "docker": dockerapi.summarize(),
        "docker_socket": dockerapi.socket_path(),
        "daemon_json_mounted": daemonconf.is_mounted(),
        "daemon_json_store": daemonconf.mount_candidates(),
        "supervisor_self": {k: v for k, v in supervisor_api.self_info().items()
                            if k in ("protected", "host_pid", "host_network", "docker_api",
                                     "version", "state", "privileged", "apparmor")},
        "kernel": mihomo.status(),
        "config": cfg,
        "state": settings.load_state(),
        "apply_result": daemonconf.read_apply_result(),
        "apply_unit": daemonconf.unit_active(),
        "no_proxy": modes.no_proxy_list(cfg),
        "local_addresses": modes.local_addresses(),
        "paths": {"static": STATIC_DIR, "static_exists": os.path.isdir(STATIC_DIR)},
    }


def _probe() -> dict:
    out = {}
    for name, args in (
        ("id", ["sh", "-c", "id"]),
        ("pwd_host", ["sh", "-c", "pwd"]),
        ("ls_etc_docker", ["sh", "-c", "ls -la /etc/docker 2>&1"]),
        ("systemctl", ["sh", "-c", "command -v systemctl systemd-run docker pgrep mount umount 2>&1"]),
        ("mounts", ["sh", "-c", "grep -E 'docker' /proc/mounts 2>&1 | head -5"]),
        ("etc_writable", ["sh", "-c", "(touch /etc/docker/.w 2>&1 && echo WRITABLE && rm -f /etc/docker/.w) || echo READONLY"]),
        ("os_release", ["sh", "-c", "head -3 /etc/os-release 2>&1"]),
    ):
        rc, o, e = hostops.run_host(args, timeout=15)
        out[name] = {"rc": rc, "out": (o or "").strip()[:500], "err": (e or "").strip()[:300]}
    return out


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def main() -> None:
    import signal  # 局部导入：仅用于优雅处理退出信号

    settings.ensure_dirs()

    # 优雅退出：Supervisor 停止插件时会发 SIGTERM，默认处理会以退出码 143 结束，
    # 并在 HA 日志里留下 "did not handle SIGTERM ... exit code 143" 告警。
    def _stop(signum, _frame):
        log(f"收到信号 {signum}，优雅退出（exit 0）", "notice", "boot")
        raise SystemExit(0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _stop)
        except Exception:
            pass

    cfg = settings.load_config()
    tok = settings.api_token()

    log(f"Net Proxy 启动；数据目录 {settings.DATA_DIR}；配置来源 {cfg.get('_source')}", "notice", "boot")
    log(f"命名空间模式：{hostops.namespace_mode()}；宿主访问：{hostops.host_available()}", "notice", "boot")
    if not hostops.host_available():
        log("警告：拿不到宿主文件系统访问权限，方式 A/B/C 都无法生效。"
            "请检查插件是否获得 host_pid: true 与 privileged: [SYS_ADMIN]。", "warning", "boot")
    log(f"直连面板令牌（仅手动发布端口时需要）：{tok}", "notice", "boot")
    log(f"内核状态：{json.dumps(mihomo.status(), ensure_ascii=False)}", "info", "boot")

    try:
        modes.recover_on_boot(cfg)
    except Exception as e:
        log(f"启动恢复流程异常：{type(e).__name__}: {e}", "error", "boot")

    try:
        modes.boot_reapply(settings.load_config())
    except Exception as e:
        log(f"开机自动落地异常：{type(e).__name__}: {e}", "error", "boot")

    health.Watchdog().start()

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    log(f"HTTP 服务监听 0.0.0.0:{PORT}（ingress 面板）", "notice", "boot")
    srv.serve_forever()


if __name__ == "__main__":
    main()
