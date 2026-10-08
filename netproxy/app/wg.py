"""WireGuard .conf 解析 → mihomo wireguard 出站参数。

选择 mihomo 的 userspace WireGuard 实现而不是 wg-quick/内核模块的原因：
  * 不需要在内核里建 tun、不需要改宿主机路由表；
  * 全局路由一旦配错会切断 Supervisor ↔ 容器通信（172.30.32.0/24），非常危险；
  * userspace 出站只影响经我们本地代理出去的流量，风险面小得多。
"""
from __future__ import annotations

import re
from typing import Optional


class WgError(Exception):
    pass


def _parse_sections(text: str) -> list:
    sections = []
    cur = None
    for raw_line in (text or "").splitlines():
        line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^\[(.+?)\]$", line, re.I)
        if m:
            cur = {"type": m.group(1).strip().lower(), "kv": {}}
            sections.append(cur)
            continue
        if cur is None:
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        cur["kv"][k.strip().lower().replace(" ", "")] = v.strip()
    return sections


def _split_list(v: Optional[str]) -> list:
    if not v:
        return []
    return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]


def _endpoint(value: str) -> tuple:
    v = value.strip()
    if not v:
        raise WgError("Peer 缺少 Endpoint")
    if v.startswith("["):  # [ipv6]:port
        host, _, port = v.rpartition("]")
        host = host.lstrip("[")
        port = port.lstrip(":")
        if not port:
            raise WgError(f"Endpoint 缺少端口：{value}")
        return host, int(port)
    if v.count(":") == 1:
        host, port = v.split(":")
        return host, int(port)
    # 只有 IPv6 没有端口
    raise WgError(f"Endpoint 缺少端口：{value}")


def parse(text: str) -> dict:
    """解析标准 WireGuard 配置，返回规范化字典。字段缺失即报错。"""
    if not (text or "").strip():
        raise WgError("WireGuard 配置为空")

    sections = _parse_sections(text)
    iface = next((s for s in sections if s["type"] == "interface"), None)
    peers = [s for s in sections if s["type"] == "peer"]
    if iface is None:
        raise WgError("缺少 [Interface] 段")
    if not peers:
        raise WgError("缺少 [Peer] 段")

    i = iface["kv"]
    private_key = i.get("privatekey") or ""
    if not private_key:
        raise WgError("[Interface] 缺少 PrivateKey")

    addresses = _split_list(i.get("address"))
    if not addresses:
        raise WgError("[Interface] 缺少 Address")

    ipv4 = [a for a in addresses if ":" not in a]
    ipv6 = [a for a in addresses if ":" in a]
    if not ipv4:
        raise WgError("[Interface] 至少要有一个 IPv4 Address（mihomo 的 wireguard 出站需要 ip）")

    dns = _split_list(i.get("dns"))
    try:
        mtu = int(i.get("mtu", "") or 0)
    except ValueError:
        mtu = 0

    out_peers = []
    for p in peers:
        kv = p["kv"]
        pub = kv.get("publickey") or ""
        if not pub:
            raise WgError("[Peer] 缺少 PublicKey")
        host, port = _endpoint(kv.get("endpoint", ""))
        allowed = _split_list(kv.get("allowedips"))
        try:
            keepalive = int(kv.get("persistentkeepalive", "") or 0)
        except ValueError:
            keepalive = 0
        out_peers.append({
            "public_key": pub,
            "preshared_key": kv.get("presharedkey") or "",
            "endpoint_host": host,
            "endpoint_port": port,
            "allowed_ips": allowed or ["0.0.0.0/0"],
            "keepalive": keepalive,
        })

    return {
        "private_key": private_key,
        "ip": ipv4[0].split("/")[0],
        "ipv6": (ipv6[0].split("/")[0] if ipv6 else ""),
        "dns": dns,
        "mtu": mtu or 1408,
        "peers": out_peers,
    }


def to_mihomo_outbound(name: str, parsed: dict) -> dict:
    """转成 mihomo 的 wireguard 出站。多 Peer 时只用第一个（mihomo 一个出站只支持单 Peer）。"""
    p = parsed["peers"][0]
    ob = {
        "name": name,
        "type": "wireguard",
        "server": p["endpoint_host"],
        "port": p["endpoint_port"],
        "ip": parsed["ip"],
        "private-key": parsed["private_key"],
        "public-key": p["public_key"],
        "allowed-ips": p["allowed_ips"],
        "mtu": parsed["mtu"],
        "udp": True,
        "remote-dns-resolve": True,
    }
    if parsed.get("ipv6"):
        ob["ipv6"] = parsed["ipv6"]
    if p.get("preshared_key"):
        ob["pre-shared-key"] = p["preshared_key"]
    if parsed.get("dns"):
        ob["dns"] = parsed["dns"]
    return ob


def summarize(text: str) -> dict:
    """给面板用：只回显非敏感信息 + 是否可用。"""
    try:
        p = parse(text)
    except WgError as e:
        return {"valid": False, "error": str(e)}
    pe = p["peers"][0]
    return {
        "valid": True,
        "ip": p["ip"],
        "mtu": p["mtu"],
        "dns": p["dns"],
        "endpoint": f"{pe['endpoint_host']}:{pe['endpoint_port']}",
        "allowed_ips": pe["allowed_ips"],
        "peer_count": len(p["peers"]),
        "has_preshared_key": bool(pe.get("preshared_key")),
    }
