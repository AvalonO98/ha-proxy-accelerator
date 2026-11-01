"""进程内 TTL 缓存（探测层专用）。

为什么需要它：面板每 5 秒轮询一次 `/api/status`，而单次 status 原先会触发

    nsenter cat daemon.json、awk /proc/mounts、ip -o addr、mihomo -v、
    parent_writable 探针 ×2、systemctl is-active、docker /info + /version ×2 …

十来个子进程与两次 Docker API 调用。在树莓派这类宿主上，这点"数据新鲜度"
换来的代价明显不成比例，而 /api/status 的结果本来就允许落后几秒。

使用约定（很重要）：

  * 只缓存**探测结果**（子进程 / HTTP 的产物），不缓存业务状态机；
  * 任何写操作落地后，调用方必须显式 `invalidate*()` —— 缓存不替调用方
    猜"什么时候该脏"，否则会把"刚写完却读到旧值"变成新的假失败源；
  * 同一个 key 的并发回源会被合并（后到的请求等先到的算完），避免面板多开
    或看门狗同时探测时把子进程数量翻倍。

时间基准用 monotonic，避免宿主校时导致缓存"永不过期"或"立刻过期"。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Tuple

#: (过期时刻, 值)
_store: Dict[str, Tuple[float, Any]] = {}
_locks: Dict[str, threading.Lock] = {}
_guard = threading.RLock()


def _lock_for(key: str) -> threading.Lock:
    with _guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def get(key: str, ttl: float, producer: Callable[[], Any]) -> Any:
    """命中则返回缓存值，否则调用 producer() 并缓存 ttl 秒。

    producer 抛异常时不缓存（异常结果不值得记住），直接向上抛。
    """
    now = time.monotonic()
    with _guard:
        hit = _store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]

    with _lock_for(key):
        # 双重检查：等锁期间可能已有别的线程回源完成
        now = time.monotonic()
        with _guard:
            hit = _store.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
        value = producer()
        with _guard:
            _store[key] = (time.monotonic() + max(0.0, ttl), value)
        return value


def put(key: str, value: Any, ttl: float) -> Any:
    with _guard:
        _store[key] = (time.monotonic() + max(0.0, ttl), value)
    return value


def peek(key: str):
    """只读查看当前是否命中（测试与诊断用），不回源。"""
    with _guard:
        hit = _store.get(key)
        if hit is None or hit[0] <= time.monotonic():
            return None
        return hit[1]


def invalidate(*keys: str) -> None:
    with _guard:
        for k in keys:
            _store.pop(k, None)


def invalidate_prefix(prefix: str) -> None:
    with _guard:
        for k in [k for k in _store if k.startswith(prefix)]:
            _store.pop(k, None)


def clear() -> None:
    with _guard:
        _store.clear()


def stats() -> dict:
    now = time.monotonic()
    with _guard:
        live = [k for k, (exp, _v) in _store.items() if exp > now]
        return {"entries": len(_store), "live": len(live), "keys": sorted(live)}
