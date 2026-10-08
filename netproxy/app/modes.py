"""编排层：把面板上的配置真正落到系统上。

一次 apply 的完整链路：
    能力检查 → (方式 B/C) 生成并启动 mihomo 内核 → 计算 daemon.json 目标内容
    → 校验 → 备份 → 原子写入 → 生效（镜像源热加载 / 代理重启 dockerd）
    → 用 dockerd 实时配置反向校验 → 失败即回滚

关于"重启 dockerd"：重启会让所有容器（包括本插件自己）重启，因此这一步交给
宿主机上独立的 systemd 单元执行，并把校验+回滚安排在重启之后（本插件重启回来的
时候由 recover_on_boot 完成），这样即使中途被杀也不会留下坏配置。
"""
from __future__ import annotations

import json
import os
import socket
import time
from typing import Optional

from . import daemonconf, dockerapi, hostops, mihomo, settings, wg
from .daemonconf import Plan
from .logbus import log

RESTART_UNITS = ("netproxy-apply.service",)


# --------------------------------------------------------------------------- #
# 期望状态
# --------------------------------------------------------------------------- #
def proxies_dict(cfg: dict) -> dict:
    p = f"http://127.0.0.1:{mihomo.PROXY_PORT}"
    return {"http-proxy": p, "https-proxy": p, "no-proxy": no_proxy_list(cfg)}


def local_addresses() -> list:
    """本机地址（容器在宿主网络命名空间里，拿到的就是宿主地址）。"""
    ips = set()
    try:
        host = socket.gethostbyname(socket.gethostname())
        if host:
            ips.add(host)
    except OSError:
        pass
    try:
        import subprocess
        r = subprocess.run(["ip", "-o", "addr", "show"], capture_output=True, timeout=5)
        for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
                ips.add(parts[3].split("/")[0])
    except Exception:
        pass
    return sorted(ips)


def no_proxy_list(cfg: dict) -> str:
    """必须直连的目标：本地、HA 内部网络、内网段。

    漏掉 172.30.32.0/23 会让 Supervisor ↔ 容器、以及插件自己访问 Supervisor API
    都被塞进代理，轻则绕远、重则自锁。
    """
    items = [
        "localhost", "127.0.0.1", "::1",
        ".local", ".local.hass.io", ".lan", "supervisor", "hassio",
        "172.30.32.0/23", "172.17.0.0/16", "10.0.0.0/8", "192.168.0.0/16",
        "100.64.0.0/10", "169.254.0.0/16",
    ]
    for ip in local_addresses():
        if ip not in items:
            items.append(ip)
    return ",".join(items)


def desired_plan(cfg: dict) -> Plan:
    """当前配置"希望"daemon.json 里托管键是什么样。"""
    mode = cfg["mode"]
    if mode == "docker_mirror":
        return Plan(
            registry_mirrors=list(cfg.get("mirrors") or []),
            proxies={},       # 明确清掉我们之前设过的代理
            need_restart=False,
            reason="Docker 加速镜像（仅 Docker Hub；热加载不中断 HA）",
        )
    # 方式 B / C：dockerd 走本地代理，覆盖所有 registry（含 ghcr.io）
    plan = Plan(
        registry_mirrors=list(cfg.get("mirrors") or []) or None,
        proxies=proxies_dict(cfg),
        need_restart=True,
        reason="上游代理 / 内置 WireGuard 出口（覆盖 ghcr.io，需要重启 dockerd 一次）",
    )
    return plan


def restore_plan(original: dict) -> Plan:
    plan = Plan(reason="关闭：把托管键还原成插件介入之前的样子")
    o = (original or {}).get("registry-mirrors") or {"present": False}
    plan.registry_mirrors = list(o["value"]) if o.get("present") else []
    o = (original or {}).get("proxies") or {"present": False}
    plan.proxies = dict(o["value"]) if o.get("present") else {}
    plan.need_restart = True  # 保守：由实际 diff 决定是否真的重启
    return plan


# --------------------------------------------------------------------------- #
# 内核
# --------------------------------------------------------------------------- #
def ensure_kernel_running(cfg: dict) -> tuple:
    """方式 B/C：生成配置并启动内核。"""
    path = mihomo.kernel_path()
    if not path:
        return False, "未找到 mihomo 内核：请在面板点「安装内核」，或把内核放到 /share/netproxy/mihomo"
    if cfg["mode"] == "upstream_proxy":
        up = cfg.get("upstream") or {}
        sub = cfg.get("subscription") or {}
        if up.get("type") not in ("http", "socks5") and not sub.get("url") and not os.path.isfile("/share/netproxy/subscription.yaml"):
            return False, "方式 B 需要填写上游代理地址（http/socks5）或订阅地址"
    if cfg["mode"] == "wireguard":
        text = (cfg.get("wireguard") or {}).get("config", "")
        if not text.strip():
            return False, "方式 C 需要粘贴 WireGuard 配置"
        try:
            wg.parse(text)
        except wg.WgError as e:
            return False, f"WireGuard 配置无法解析：{e}"

    ok, payload = mihomo.write_config(cfg)
    if not ok:
        return False, payload
    ok, msg = mihomo.test_config()
    if not ok:
        return False, f"mihomo 配置校验失败：{msg}"
    if mihomo.alive():
        ok, msg = mihomo.stop()
        if not ok:
            return False, msg
    ok, msg = mihomo.start()
    if not ok:
        return False, msg
    return True, "内核已启动"


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
def _expected_from(new_obj: dict, plan: Plan) -> dict:
    return {
        "registry_mirrors": new_obj.get("registry-mirrors") if plan.registry_mirrors is not None else None,
        "proxies": new_obj.get("proxies") if plan.proxies is not None else None,
    }


def apply(cfg: dict, job: Optional[dict] = None) -> dict:
    """把配置落到系统。cfg['enabled'] 为 False 时执行还原。"""
    if not hostops.host_available():
        raise RuntimeError(
            "无法访问宿主机文件系统。请确认插件配置里有 host_pid: true + privileged: [SYS_ADMIN]，"
            f"当前模式：{hostops.namespace_mode()}"
        )

    cur = daemonconf.read_current()
    if not cur["ok"]:
        raise RuntimeError(cur.get("error") or "读取 daemon.json 失败")
    log(f"当前 daemon.json：existed={cur['existed']} 顶层键={sorted((cur['data'] or {}).keys())}",
        "debug", "daemon.json")

    # ---- 1. 内核 ----
    if cfg["enabled"] and cfg["mode"] in ("upstream_proxy", "wireguard"):
        ok, msg = ensure_kernel_running(cfg)
        if not ok:
            raise RuntimeError(msg)
        log(msg, "notice", "mihomo")
    else:
        if mihomo.alive():
            mihomo.stop()
            log("已停止 mihomo 内核（当前方式不需要它）", "info", "mihomo")

    # ---- 2. 备份/原值 ----
    st = settings.load_state()
    applied = st.get("applied") or {}
    original = applied.get("original")
    backup_id = applied.get("backup_id")
    if not original:
        bk = daemonconf.backup(cur, "before-first-apply")
        original = bk["managed_original"]
        backup_id = bk["id"]

    plan = desired_plan(cfg) if cfg["enabled"] else restore_plan(original)
    new_obj = daemonconf.build(cur["data"] or {}, plan)
    ok, msg = daemonconf.validate(new_obj)
    if not ok:
        raise RuntimeError(f"生成的 daemon.json 未通过校验：{msg}")

    changed = daemonconf.diff_keys(cur["data"] or {}, new_obj)
    if not changed:
        if not cfg["enabled"]:
            _save_applied(st, active=False, original=original, backup_id=backup_id,
                          expected={"registry_mirrors": None, "proxies": None}, plan=plan,
                          pre_raw=cur.get("raw"), pre_existed=cur.get("existed"),
                          pending=False, mode=cfg["mode"])
            return {"state": "unchanged", "message": "已是插件介入前的状态，无需改动", "changed": []}
        return {"state": "unchanged", "message": "配置没有变化，无需重新应用", "changed": []}

    need_restart = "proxies" in changed
    expected = _expected_from(new_obj, plan)
    log(f"将要修改 daemon.json 的键：{changed}；{'需要重启 dockerd' if need_restart else '仅需 SIGHUP 热加载'}",
        "notice", "daemon.json")

    # 先把"待校验"状态落盘：万一插件在重启中被杀，回来还能接着校验/回滚
    _save_applied(st, active=cfg["enabled"], original=original, backup_id=backup_id,
                  expected=expected, plan=plan, pre_raw=cur.get("raw"),
                  pre_existed=cur.get("existed"), pending=need_restart, mode=cfg["mode"])

    # ---- 3. 写 ----
    ok, wmsg = daemonconf.write_daemon_json(new_obj)
    if not ok:
        raise RuntimeError(f"写入 daemon.json 失败：{wmsg}")
    if wmsg.startswith("bind:"):
        log(f"HAOS 的 /etc 只读，已改用 bind mount 落地：{wmsg[5:]}", "notice", "daemon.json")

    # ---- 4. 生效 ----
    if need_restart:
        ok, msg = daemonconf.restart_dockerd_detached(
            int(cfg["apply_timeout_seconds"]),
            backup_raw=cur.get("raw"), backup_existed=bool(cur.get("existed")))
        if not ok:
            log("重启 dockerd 未能启动，立刻回滚文件", "error", "daemon.json")
            daemonconf.restore_raw(cur.get("raw"), cur.get("existed"))
            raise RuntimeError(f"重启 dockerd 失败，已回滚：{msg}")
        log(msg, "notice", "daemon.json")
        return {
            "state": "restarting",
            "message": "dockerd 正在重启（所有容器会一起重启，HA 会短暂中断）。"
                       "重启完成后插件会自动校验并在失败时回滚。",
            "changed": changed, "need_restart": True, "expected": expected,
        }

    ok, msg = daemonconf.sighup_dockerd()
    if not ok:
        daemonconf.restore_raw(cur.get("raw"), cur.get("existed"))
        raise RuntimeError(f"SIGHUP 失败，已回滚：{msg}")
    log(msg, "info", "daemon.json")
    time.sleep(1.5)

    v = daemonconf.verify(expected.get("registry_mirrors"), expected.get("proxies"))
    if not v["ok"]:
        log(f"校验未通过：{v.get('problems')} → 回滚", "error", "daemon.json")
        daemonconf.restore_raw(cur.get("raw"), cur.get("existed"))
        daemonconf.sighup_dockerd()
        raise RuntimeError(f"配置校验未通过，已回滚：{v.get('problems')}")

    _save_applied(settings.load_state(), active=cfg["enabled"], original=original,
                  backup_id=backup_id, expected=expected, plan=plan,
                  pre_raw=cur.get("raw"), pre_existed=cur.get("existed"),
                  pending=False, mode=cfg["mode"], write_mode=wmsg)
    settings.push_history("apply", f"方式 {cfg['mode']} 已生效（热加载）",
                          {"changed": changed, "expected": expected, "write": wmsg})
    return {"state": "applied", "message": "已生效并校验通过", "changed": changed,
            "verify": v, "expected": expected, "write_mode": wmsg}


def _save_applied(st: dict, *, active: bool, original: dict, backup_id: Optional[str],
                  expected: dict, plan: Plan, pre_raw, pre_existed, pending: bool,
                  mode: str, write_mode: str = "") -> None:
    st["applied"] = {
        "active": bool(active),
        "mode": mode,
        "backup_id": backup_id,
        "original": original,
        "expected": expected,
        "desired": {"registry_mirrors": plan.registry_mirrors, "proxies": plan.proxies},
        "reason": plan.reason,
        "pre_raw": pre_raw,
        "pre_existed": pre_existed,
        "pending_verify": bool(pending),
        "write_mode": write_mode,
        "applied_at": time.time(),
    }
    settings.save_state(st)


# --------------------------------------------------------------------------- #
# 开机自愈
# --------------------------------------------------------------------------- #
def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def boot_reapply(cfg: dict) -> None:
    """插件（随 HA/宿主重启）启动后，把配置重新落地。

    为什么必须做：HAOS 的 /etc 只读，我们是用 bind mount 覆盖 daemon.json 的，
    而 bind mount 不跨重启 —— 重启后 dockerd 又会用回 OS 自带的配置。

    为什么不会重启循环：写文件是幂等的；只有"文件内容确实变了"才会触发重启，
    落地完成后再次检查就是"无变化"。另外这里用内核 boot_id 做闸门，同一次开机内
    最多触发一次"需要重启 dockerd"的落地。
    """
    if not hostops.host_available():
        return
    if not cfg.get("enabled"):
        if daemonconf.is_mounted():
            daemonconf.release_bind_mount()
            log("总开关关闭，已卸载 daemon.json 的 bind mount，恢复 OS 原始配置", "notice", "boot")
        return

    st = settings.load_state()
    boot_id = _boot_id()
    need_restart_mode = cfg.get("mode") in ("upstream_proxy", "wireguard")
    if need_restart_mode and st.get("last_apply_boot") == boot_id:
        live = dockerapi.summarize()
        if not (live.get("http_proxy") or live.get("https_proxy")):
            log("本次开机已尝试过需要重启 dockerd 的落地，跳过以避免重启循环；"
                "如需立即生效请在面板点「应用并生效」。", "warning", "boot")
            return

    try:
        res = apply(cfg)
        st = settings.load_state()
        st["last_apply_boot"] = boot_id
        settings.save_state(st)
        log(f"开机自动落地：{res.get('state')} · {res.get('message', '')}", "notice", "boot")
    except Exception as e:
        log(f"开机自动落地失败：{type(e).__name__}: {e}（可在面板手动应用）", "error", "boot")


# --------------------------------------------------------------------------- #
# 重启后校验（插件自己也被重启了）
# --------------------------------------------------------------------------- #
def recover_on_boot(cfg: dict) -> None:
    st = settings.load_state()
    applied = st.get("applied") or {}
    res = daemonconf.read_apply_result()

    if applied.get("pending_verify"):
        log("检测到上次应用在等待 dockerd 重启后的校验，开始校验…", "notice", "recover")
        time.sleep(3)
        expected = applied.get("expected") or {}
        v = daemonconf.verify(expected.get("registry_mirrors"), expected.get("proxies"))
        if v["ok"]:
            applied["pending_verify"] = False
            st["applied"] = applied
            settings.save_state(st)
            settings.push_history("apply", f"方式 {applied.get('mode')} 已生效（重启后校验通过）",
                                  {"verify": v})
            log(f"重启后校验通过：{json.dumps(v.get('live'), ensure_ascii=False)}", "notice", "recover")
        else:
            log(f"重启后校验失败：{v.get('problems')} → 回滚", "error", "recover")
            pre_restore = daemonconf.read_current()
            daemonconf.restore_raw(applied.get("pre_raw"), bool(applied.get("pre_existed")))
            applied["pending_verify"] = False
            applied["active"] = False
            applied["rollback_at"] = time.time()
            st["applied"] = applied
            settings.save_state(st)
            try:
                cur_cfg = settings.load_config()
                cur_cfg["enabled"] = False
                settings.save_config(cur_cfg)
            except Exception:
                pass
            settings.push_history("rollback", "重启后校验失败，已自动回滚并关闭总开关",
                                  {"problems": v.get("problems")})
            # 再重启一次让回滚内容生效（异步执行，避免卡住启动）
            daemonconf.restart_dockerd_detached(
                int(cfg.get("apply_timeout_seconds", 180)),
                backup_raw=pre_restore.get("raw"), backup_existed=bool(pre_restore.get("existed")))

    if res:
        log(f"上次宿主侧重启结果：{json.dumps(res, ensure_ascii=False)}", "info", "recover")


# --------------------------------------------------------------------------- #
# 状态与测试
# --------------------------------------------------------------------------- #
def status(cfg: dict) -> dict:
    st = settings.load_state()
    applied = st.get("applied") or {}
    cur = daemonconf.read_current()
    d = dockerapi.summarize()

    expected = applied.get("expected") or {}
    # 仅在"插件处于开启状态"时做通过/不通过判定：关闭时 dockerd 里可能保留着
    # 用户自己原本就有的镜像源，那不是错误。
    live_verify = None
    if d.get("available") and applied.get("active"):
        live_verify = daemonconf.verify(expected.get("registry_mirrors"), expected.get("proxies"))

    kern = mihomo.status()
    plan = None
    try:
        plan = desired_plan(cfg) if cfg["enabled"] else None
    except Exception:
        plan = None

    up = cfg.get("upstream") or {}
    return {
        "enabled": bool(cfg["enabled"]),
        "mode": cfg["mode"],
        "config_source": cfg.get("_source"),
        "capability": hostops.capability_report([daemonconf.DAEMON_JSON, mihomo._SHARE]),
        "daemon_json": {
            "path": cur.get("path"),
            "existed": cur.get("existed"),
            "ok": cur.get("ok"),
            "error": cur.get("error"),
            "data": cur.get("data"),
            "bind_mounted": daemonconf.is_mounted(),
            "write_mode": applied.get("write_mode") or "",
            "store_candidates": daemonconf.mount_candidates(),
            "managed_original": daemonconf.original_managed(cur.get("data") or {}) if cur.get("ok") else None,
        },
        "desired": ({"registry_mirrors": plan.registry_mirrors, "proxies": plan.proxies,
                     "need_restart": plan.need_restart, "reason": plan.reason} if plan else None),
        "docker": d,
        "verify": live_verify,
        "kernel": kern,
        "kernel_arch": mihomo._arch(),
        "wireguard": (wg.summarize((cfg.get("wireguard") or {}).get("config", ""))
                      if cfg["mode"] == "wireguard" else None),
        "upstream": {"type": up.get("type"), "url": up.get("url"),
                     "has_auth": bool(up.get("username") or up.get("password"))},
        "github_mirrors": cfg.get("github_mirrors") or [],
        "kernel_version": cfg.get("kernel_version") or "",
        "subscription_configured": bool((cfg.get("subscription") or {}).get("url"))
                                   or os.path.isfile("/share/netproxy/subscription.yaml"),
        "applied": applied,
        "history": st.get("history") or [],
        "backups": daemonconf.list_backups(),
        "apply_result": daemonconf.read_apply_result(),
        "apply_unit_active": daemonconf.unit_active(),
        "no_proxy": no_proxy_list(cfg),
        "restart_units": list(RESTART_UNITS),
    }


def connectivity_test(cfg: dict) -> dict:
    """面板上的「检测」按钮：分别给出直连与走代理的结果，以及各类镜像源可达性。"""
    targets = [
        ("ghcr.io", "https://ghcr.io/v2/"),
        ("docker.io 官方", "https://registry-1.docker.io/v2/"),
    ]
    out = {"direct": [], "via_proxy": [], "mirrors": [], "github": [],
           "kernel": None, "timestamp": time.time()}

    for name, url in targets:
        r = mihomo.http_fetch(url, via_proxy=False, timeout=12)
        r["name"] = name
        out["direct"].append(r)

    if mihomo.alive():
        out["kernel"] = mihomo.delay_test()
        for name, url in targets:
            r = mihomo.http_fetch(url, via_proxy=True, timeout=15)
            r["name"] = name
            out["via_proxy"].append(r)

    # Docker 镜像源
    for m in (cfg.get("mirrors") or []):
        r = mihomo.probe_url(m.rstrip("/") + "/v2/", method="GET", timeout=12)
        r["name"] = m
        out["mirrors"].append(r)

    # GitHub 直连 + 自定义加速源（用真实内核资源地址做 HEAD 探测，不下载正文）
    ok, tag, official, err = mihomo.official_asset_url(cfg.get("kernel_version") or "")
    if not ok:
        official = "https://github.com/MetaCubeX/mihomo/releases"
    r = mihomo.probe_url("https://api.github.com/", timeout=10)
    r["name"] = "api.github.com 直连"
    out["github"].append(r)
    r = mihomo.probe_url(official, timeout=15)
    r["name"] = f"GitHub 直连 {tag or ''}".strip()
    out["github"].append(r)
    for u, m in mihomo.apply_mirrors(official, cfg.get("github_mirrors") or []):
        r = mihomo.probe_url(u, timeout=15)
        r["name"] = f"加速源 {m}"
        out["github"].append(r)

    return out
