"""WireGuard .conf 解析测试。"""
from __future__ import annotations

from _fixtures import VALID_WG
from netproxy_app import wg


def test_parse_valid():
    p = wg.parse(VALID_WG)
    assert p["ip"] == "10.7.0.2", p
    assert p["ipv6"] == "fd00::2", p
    assert p["mtu"] == 1420, p
    assert p["dns"] == ["1.1.1.1", "8.8.8.8"], p
    assert p["private_key"].startswith("aGVsbG8")
    pe = p["peers"][0]
    assert pe["endpoint_host"] == "vpn.example.com"
    assert pe["endpoint_port"] == 51820
    assert pe["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert pe["keepalive"] == 25
    assert pe["preshared_key"].startswith("cHJl")


def test_parse_missing_fields_raise():
    cases = [
        ("", "空"),
        ("[Interface]\nPrivateKey = x\nAddress = 10.0.0.2/32\n", "缺少 [Peer]"),
        ("[Peer]\nPublicKey = y\nEndpoint = h:1\n", "缺少 [Interface]"),
        ("[Interface]\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = y\nEndpoint = h:1\n", "PrivateKey"),
        ("[Interface]\nPrivateKey = x\n[Peer]\nPublicKey = y\nEndpoint = h:1\n", "Address"),
        ("[Interface]\nPrivateKey = x\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = y\n", "Endpoint"),
        ("[Interface]\nPrivateKey = x\nAddress = 10.0.0.2/32\n[Peer]\nEndpoint = h:1\n", "PublicKey"),
    ]
    for text, frag in cases:
        try:
            wg.parse(text)
        except wg.WgError as e:
            assert frag in str(e), (frag, str(e), text)
        else:
            raise AssertionError(f"应当报错（{frag}）：{text!r}")


def test_endpoint_ipv6_and_bad_port():
    t = VALID_WG.replace("vpn.example.com:51820", "[2001:db8::1]:51820")
    assert wg.parse(t)["peers"][0]["endpoint_host"] == "2001:db8::1"

    t2 = VALID_WG.replace("Endpoint = vpn.example.com:51820", "Endpoint = 2001:db8::1")
    try:
        wg.parse(t2)
    except wg.WgError:
        pass
    else:
        raise AssertionError("IPv6 无端口应当报错")


def test_comments_and_semicolons_ignored():
    text = VALID_WG.replace("MTU = 1420", "MTU = 1420  # 注释\n; 整行注释")
    assert wg.parse(text)["mtu"] == 1420


def test_to_mihomo_outbound():
    ob = wg.to_mihomo_outbound("WG", wg.parse(VALID_WG))
    assert ob["type"] == "wireguard"
    assert ob["server"] == "vpn.example.com" and ob["port"] == 51820
    assert ob["private-key"].startswith("aGVsbG8")
    assert ob["public-key"].startswith("d29ybGQ")
    assert ob["pre-shared-key"].startswith("cHJl")
    assert ob["allowed-ips"] == ["0.0.0.0/0", "::/0"]
    assert ob["ipv6"] == "fd00::2"
    assert ob["dns"] == ["1.1.1.1", "8.8.8.8"]
    assert ob["mtu"] == 1420
    assert ob["udp"] is True


def test_summarize():
    s = wg.summarize(VALID_WG)
    assert s["valid"] is True
    assert s["endpoint"] == "vpn.example.com:51820"
    assert s["peer_count"] == 1
    assert s["has_preshared_key"] is True
    bad = wg.summarize("这不是配置")
    assert bad["valid"] is False and bad["error"]
