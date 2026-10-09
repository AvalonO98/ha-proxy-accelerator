"""mihomo 配置生成测试（生成的是受控 YAML 文本，不做通用序列化）。"""
from __future__ import annotations

from _fixtures import VALID_WG
from netproxy_app import mihomo, settings


def _cfg(**kw):
    base = {"mode": "upstream_proxy", "upstream": {"type": "http", "url": "http://127.0.0.1:10808"}}
    base.update(kw)
    return settings.normalize(base)


def test_split_hostport_variants():
    assert mihomo._split_hostport("http://1.2.3.4:7890")[:2] == ("1.2.3.4", 7890)
    assert mihomo._split_hostport("socks5://user:pass@h.example:1080")[:2] == ("h.example", 1080)
    assert mihomo._split_hostport("h:1080")[:2] == ("h", 1080)
    assert mihomo._split_hostport("h:1080/")[:2] == ("h", 1080)
    assert mihomo._split_hostport("")[2]
    assert mihomo._split_hostport("nope")[2]
    assert mihomo._split_hostport("h:notaport")[2]


def test_generate_upstream_http():
    text, err = mihomo.generate_config(_cfg())
    assert not err, err
    assert "mixed-port: 7890" in text
    assert 'bind-address: "127.0.0.1"' in text          # 只监听回环，不暴露到局域网
    assert 'server: "127.0.0.1"' in text and "port: 10808" in text
    assert 'type: "http"' in text
    # 自锁防护规则必须在
    assert "IP-CIDR,172.30.32.0/23,DIRECT,no-resolve" in text
    assert "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve" in text
    assert "MATCH,PROXY" in text


def test_generate_direct_egress_without_upstream():
    """上游类型 = none：允许生成"本地代理 + 直连出口"的配置（不需要外部上游）。"""
    cfg = _cfg(upstream={"type": "none", "url": ""})
    text, err = mihomo.generate_config(cfg)
    assert not err, err
    assert "proxies: []" in text          # 没有真正的出站代理
    assert "MATCH,PROXY" in text
    assert 'type: "select"' in text
    # 自锁防护规则同样必须在
    assert "IP-CIDR,172.30.32.0/23,DIRECT,no-resolve" in text


def test_generate_upstream_with_auth():
    # 显式字段
    cfg = _cfg(upstream={"type": "socks5", "url": "socks5://10.0.0.9:1080",
                         "username": "u", "password": "p"})
    text, err = mihomo.generate_config(cfg)
    assert not err, err
    assert 'type: "socks5"' in text
    assert 'username: "u"' in text and 'password: "p"' in text
    assert 'server: "10.0.0.9"' in text and "port: 1080" in text

    # 凭据内嵌在 URL 里也必须被识别（用户只贴 URL 的场景）
    cfg2 = _cfg(upstream={"type": "socks5", "url": "socks5://user2:pass2@10.0.0.8:1080",
                          "username": "", "password": ""})
    text2, err2 = mihomo.generate_config(cfg2)
    assert not err2, err2
    assert 'username: "user2"' in text2 and 'password: "pass2"' in text2
    assert 'server: "10.0.0.8"' in text2

    # 面板里单独填的凭据优先于 URL 内嵌的
    cfg3 = _cfg(upstream={"type": "http", "url": "http://embedded:xx@10.0.0.7:8080",
                          "username": "panel", "password": "secret"})
    text3, err3 = mihomo.generate_config(cfg3)
    assert not err3, err3
    assert 'username: "panel"' in text3 and 'password: "secret"' in text3


def test_parse_upstream_variants():
    assert mihomo._parse_upstream("http://u:p@h:1")[:4] == ("h", 1, "u", "p")
    assert mihomo._parse_upstream("h:2")[:4] == ("h", 2, "", "")
    assert mihomo._parse_upstream("")[4]
    assert mihomo._parse_upstream("h:abc")[4]


def test_generate_requires_upstream():
    """type=http 却没填地址 → 必须报错。
    （type=none 自 0.1.9 起是合法配置「直连出口」，见 direct egress 测试。）"""
    text, err = mihomo.generate_config(_cfg(upstream={"type": "http", "url": ""}))
    assert text == "" and err and "上游代理" in err


def test_generate_rejects_bad_upstream_url():
    text, err = mihomo.generate_config(_cfg(upstream={"type": "http", "url": "not-a-host-port"}))
    assert text == "" and "无法解析" in err


def test_generate_wireguard():
    cfg = _cfg(mode="wireguard", wireguard={"config": VALID_WG})
    text, err = mihomo.generate_config(cfg)
    assert not err, err
    assert 'type: "wireguard"' in text
    assert 'server: "vpn.example.com"' in text and "port: 51820" in text
    assert 'private-key: "aGVsbG8' in text
    assert 'pre-shared-key: "cHJl' in text
    assert 'ip: "10.7.0.2"' in text
    assert "mtu: 1420" in text
    assert 'allowed-ips:' in text
    assert "  - \"::/0\"" in text or '- "::/0"' in text


def test_generate_wireguard_requires_config():
    cfg = _cfg(mode="wireguard", wireguard={"config": ""})
    text, err = mihomo.generate_config(cfg)
    assert text == "" and "WireGuard" in err


def test_generate_wireguard_bad_config():
    cfg = _cfg(mode="wireguard", wireguard={"config": "[Interface]\nPrivateKey = x\n"})
    text, err = mihomo.generate_config(cfg)
    assert text == "" and "解析" in err


def test_generate_subscription_provider():
    cfg = _cfg(subscription={"url": "https://example.com/sub.yaml", "name": "s"})
    text, err = mihomo.generate_config(cfg)
    assert not err, err
    assert "proxy-providers:" in text
    assert 'url: "https://example.com/sub.yaml"' in text
    assert "use:" in text and "health-check:" in text
    assert 'path: "./providers/sub.yaml"' in text


def test_docker_mirror_mode_needs_no_kernel():
    text, err = mihomo.generate_config(settings.normalize({"mode": "docker_mirror"}))
    assert text == "" and err and "不需要" in err


def test_dns_block_from_config():
    cfg = _cfg(dns=["9.9.9.9", "1.1.1.1"])
    text, err = mihomo.generate_config(cfg)
    assert not err, err
    assert '    - "9.9.9.9"' in text and '    - "1.1.1.1"' in text


def test_yaml_quoting_of_special_characters():
    cfg = _cfg(upstream={"type": "http", "url": "http://1.2.3.4:8080",
                         "username": 'we"ird\\name', "password": "  spaced  "})
    text, err = mihomo.generate_config(cfg)
    assert not err, err
    assert 'username: "we\\"ird\\\\name"' in text
    assert 'password: "  spaced  "' in text


def test_write_config_to_disk_and_test_config_without_kernel():
    cfg = _cfg()
    ok, payload = mihomo.write_config(cfg)
    assert ok, payload
    assert "mixed-port" in payload
    import os
    assert os.path.exists(mihomo.config_path())
    ok2, msg2 = mihomo.test_config()
    assert ok2 is False and "内核未安装" in msg2
