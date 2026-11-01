"""探测层缓存的行为约定。

这里守的不是"缓存能命中"，而是几条容易写错就变成新 bug 的性质：
  * 过期后必须重新回源（否则改完配置面板一直显示旧值）；
  * invalidate_prefix 必须能整族失效（写操作后靠它）；
  * producer 抛异常不缓存（否则一次瞬时失败会被记住整个 TTL）；
  * 并发回源只算一次（面板多开 / 看门狗同时探测时不吃双份子进程）。
"""
from __future__ import annotations

import threading
import time

from netproxy_app import cache


def test_hit_within_ttl_and_miss_after():
    calls = []

    def producer():
        calls.append(1)
        return {"n": len(calls)}

    cache.clear()
    a = cache.get("t.basic", 0.3, producer)
    b = cache.get("t.basic", 0.3, producer)
    assert a == b == {"n": 1}
    assert len(calls) == 1

    time.sleep(0.35)
    c = cache.get("t.basic", 0.3, producer)
    assert c == {"n": 2} and len(calls) == 2


def test_zero_ttl_never_hits():
    calls = []

    def producer():
        calls.append(1)
        return len(calls)

    cache.clear()
    assert cache.get("t.zero", 0, producer) == 1
    assert cache.get("t.zero", 0, producer) == 2


def test_invalidate_and_prefix():
    cache.clear()
    cache.get("docker.summarize", 30, lambda: "old")
    cache.get("docker.extra", 30, lambda: "old")
    cache.get("daemon.backups:20", 30, lambda: "old")

    assert cache.peek("docker.summarize") == "old"
    cache.invalidate_prefix("docker.")
    assert cache.peek("docker.summarize") is None
    assert cache.peek("docker.extra") is None
    assert cache.peek("daemon.backups:20") == "old"

    cache.invalidate("daemon.backups:20")
    assert cache.peek("daemon.backups:20") is None


def test_producer_exception_is_not_cached():
    cache.clear()
    state = {"n": 0}

    def boom():
        state["n"] += 1
        raise RuntimeError("probe failed")

    for _ in range(2):
        try:
            cache.get("t.boom", 30, boom)
        except RuntimeError:
            pass
    assert state["n"] == 2
    assert cache.peek("t.boom") is None


def test_concurrent_callers_share_one_probe():
    cache.clear()
    calls = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(2)          # 让所有线程都先撞到"未命中"
        return "value"

    results = []

    def worker():
        results.append(cache.get("t.stampede", 30, slow))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.15)
    gate.set()
    for t in threads:
        t.join(5)

    assert results == ["value"] * 5
    assert len(calls) == 1


def test_stats_reports_live_entries():
    cache.clear()
    cache.get("t.live", 30, lambda: 1)
    cache.get("t.dead", 0, lambda: 2)
    st = cache.stats()
    assert "t.live" in st["keys"] and "t.dead" not in st["keys"]
    assert st["live"] == 1
