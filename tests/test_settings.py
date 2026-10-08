"""配置/状态存储测试。"""
from __future__ import annotations

import os

from netproxy_app import settings


def _clean():
    try:
        if settings.CONFIG_FILE.exists():
            os.remove(settings.CONFIG_FILE)
    except OSError:
        pass


def test_normalize_defaults():
    c = settings.normalize({})
    assert c["mode"] == "docker_mirror"
    assert c["enabled"] is False
    assert c["mirrors"] and c["dns"] and c["github_mirrors"]
    assert c["apply_timeout_seconds"] == 180
    assert c["upstream"]["type"] == "none"


def test_normalize_repairs_bad_values():
    c = settings.normalize({
        "mode": "bogus",
        "mirrors": "https://only-one",
        "github_mirrors": "https://gh/",
        "dns": [],
        "health_check_seconds": 999999,
        "apply_timeout_seconds": 1,
        "enabled": "yes",
        "upstream": {"type": "pptp"},
        "log_level": "shout",
        "kernel_version": None,
    })
    assert c["mode"] == "docker_mirror"
    assert c["mirrors"] == ["https://only-one"]
    assert c["github_mirrors"] == ["https://gh/"]
    assert c["dns"] == settings.DEFAULTS["dns"]
    assert c["health_check_seconds"] == 3600
    assert c["apply_timeout_seconds"] == 30
    assert c["enabled"] is True
    assert c["upstream"]["type"] == "none"
    assert c["log_level"] == "info"
    assert c["kernel_version"] == ""


def test_normalize_strips_blank_entries():
    c = settings.normalize({"mirrors": ["https://a", "  ", ""], "github_mirrors": ["", " "], "dns": [" 1.1.1.1 "]})
    assert c["mirrors"] == ["https://a"]
    assert c["github_mirrors"] == []
    assert c["dns"] == ["1.1.1.1"]


def test_save_load_roundtrip():
    _clean()
    settings.save_config({
        "mode": "upstream_proxy",
        "mirrors": ["https://x"],
        "github_mirrors": ["https://ghfast.top/"],
        "kernel_version": "v1.19.32",
        "enabled": True,
    })
    got = settings.load_config()
    assert got["mode"] == "upstream_proxy"
    assert got["mirrors"] == ["https://x"]
    assert got["enabled"] is True
    assert got["github_mirrors"] == ["https://ghfast.top/"]
    assert got["kernel_version"] == "v1.19.32"
    assert got["_source"] == "config.json"
    # 落盘的是干净结构，不含内部字段
    raw = settings.CONFIG_FILE.read_text(encoding="utf-8")
    assert "_source" not in raw
    _clean()


def test_mask_hides_secrets():
    m = settings.mask({
        "upstream": {"password": "supersecret", "url": "http://x"},
        "wireguard": {"config": "PrivateKey = abc"},
    })
    assert "supersecret" not in str(m)
    assert "***" in m["upstream"]["password"]
    assert "***" in m["wireguard"]["config"]
    assert m["upstream"]["url"] == "http://x"


def test_state_and_history():
    settings.save_state({"applied": {"active": True}})
    st = settings.load_state()
    assert st["applied"]["active"] is True
    assert "history" in st and isinstance(st["history"], list)
    settings.push_history("apply", "单元测试", {"k": 1})
    st = settings.load_state()
    assert st["history"][0]["detail"] == "单元测试"
    assert st["history"][0]["extra"] == {"k": 1}


def test_api_token_stable():
    t1 = settings.api_token()
    t2 = settings.api_token()
    assert t1 and t1 == t2 and len(t1) > 20
