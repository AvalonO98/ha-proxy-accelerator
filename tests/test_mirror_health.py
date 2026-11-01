"""镜像源健康过滤（0.1.14 的"加速"核心）。

dockerd 按顺序尝试 registry-mirrors，遇到不响应的源要先等它超时 —— 所以
"把死源写进去"不只是没用，而是让**每一次** pull 都变慢。这里守三件事：
  * 可达的源按实测延迟升序（快的排前面）；
  * 不可达的源被剔除，并带出原因；
  * 输入为空/全是死源时不抛异常（由调用方决定拒绝还是继续）。
"""
from __future__ import annotations

from netproxy_app import modes


def _fake_probe(table):
    """table: {url: (ok, ms, error)}"""
    def probe(url, method="HEAD", via_proxy=False, timeout=12.0):
        ok, ms, err = table[url]          # _probe_mirror 已经把 /v2/ 拼好了
        return ({"ok": True, "status": 200, "ms": ms, "url": url} if ok
                else {"ok": False, "error": err, "url": url})
    return probe


def test_rank_orders_by_latency():
    table = {
        "https://slow/v2/": (True, 420, None),
        "https://fast/v2/": (True, 35, None),
        "https://mid/v2/": (True, 180, None),
    }
    origin = modes.mihomo.probe_url
    modes.mihomo.probe_url = _fake_probe(table)
    try:
        ranked, dropped = modes.rank_mirrors(
            ["https://slow", "https://fast", "https://mid"])
    finally:
        modes.mihomo.probe_url = origin
    assert ranked == ["https://fast", "https://mid", "https://slow"]
    assert dropped == []


def test_rank_drops_unreachable_with_reason():
    table = {
        "https://good/v2/": (True, 60, None),
        "https://dead/v2/": (False, None, "URLError: timed out"),
    }
    origin = modes.mihomo.probe_url
    modes.mihomo.probe_url = _fake_probe(table)
    try:
        ranked, dropped = modes.rank_mirrors(["https://dead", "https://good"])
    finally:
        modes.mihomo.probe_url = origin
    assert ranked == ["https://good"]
    assert len(dropped) == 1 and dropped[0][0] == "https://dead"
    assert "timed out" in dropped[0][1]


def test_rank_handles_empty_and_all_dead():
    assert modes.rank_mirrors([]) == ([], [])
    assert modes.rank_mirrors(None) == ([], [])

    origin = modes.mihomo.probe_url
    modes.mihomo.probe_url = _fake_probe({"https://x/v2/": (False, None, "boom")})
    try:
        ranked, dropped = modes.rank_mirrors(["https://x"])
    finally:
        modes.mihomo.probe_url = origin
    assert ranked == [] and len(dropped) == 1


def test_rank_ignores_blank_entries():
    origin = modes.mihomo.probe_url
    modes.mihomo.probe_url = _fake_probe({"https://a/v2/": (True, 10, None)})
    try:
        ranked, _ = modes.rank_mirrors(["  ", "https://a", ""])
    finally:
        modes.mihomo.probe_url = origin
    assert ranked == ["https://a"]


def test_mirror_healthcheck_default_on_and_togglable():
    from netproxy_app import settings

    default = settings.normalize({})
    assert default["mirror_healthcheck"] is True
    assert settings.normalize({"mirror_healthcheck": False})["mirror_healthcheck"] is False
    # 字符串等脏值收敛成布尔，绝不抛
    assert settings.normalize({"mirror_healthcheck": "yes"})["mirror_healthcheck"] is True


# --------------------------------------------------------------------------- #
# 重载落定后再判定（0.1.15：修掉"其实已生效却被判失败 → 回滚"）
# --------------------------------------------------------------------------- #
def test_verify_settled_retries_until_ok():
    calls = []

    def fake(expected_mirrors, expected_proxies, summary=None):
        calls.append(1)
        ok = len(calls) >= 3
        return {"ok": ok, "problems": [] if ok else ["还没生效"]}

    origin = modes.daemonconf.verify
    modes.daemonconf.verify = fake
    try:
        v = modes.verify_settled([], {}, tries=5, interval=0)
    finally:
        modes.daemonconf.verify = origin
    assert v["ok"] is True
    assert len(calls) == 3          # 前两次失败不算数，第三次通过就收工


def test_verify_settled_gives_up_and_reports_last_result():
    calls = []

    def fake(expected_mirrors, expected_proxies, summary=None):
        calls.append(1)
        return {"ok": False, "problems": [f"第{len(calls)}次"]}

    origin = modes.daemonconf.verify
    modes.daemonconf.verify = fake
    try:
        v = modes.verify_settled([], {}, tries=4, interval=0)
    finally:
        modes.daemonconf.verify = origin
    assert v["ok"] is False and v["problems"] == ["第4次"]
    assert len(calls) == 4
