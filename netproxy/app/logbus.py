"""内存日志环形缓冲，供面板轮询。

不落盘：日志量小、面板是唯一消费者；落盘反而会在 /data 里堆垃圾。

级别过滤：`log_level` 之前只用来生成 mihomo 的配置，插件自己的日志一律无脑
塞进缓冲 —— 而每个 HTTP 访问都会打一条 debug，面板里 90% 都是噪音。现在按
配置的最低级别过滤（默认 info），阈值每 5 秒重新读一次，改了配置无需重启。
"""
from __future__ import annotations

import collections
import threading
import time

LEVELS = ("trace", "debug", "info", "notice", "warning", "error", "fatal")
_RANK = {name: i for i, name in enumerate(LEVELS)}

#: 阈值缓存：上次读取时刻 + 当时的最低级别序号，避免每条日志都读一次配置
_threshold = {"at": 0.0, "rank": _RANK["info"]}
THRESHOLD_TTL = 5.0


def _threshold_rank() -> int:
    now = time.monotonic()
    if now - _threshold["at"] < THRESHOLD_TTL:
        return _threshold["rank"]
    rank = _RANK["info"]
    try:
        from . import settings  # 延迟导入：避免 settings ← logbus 的循环依赖
        level = str(settings.load_config().get("log_level") or "info")
        rank = _RANK.get(level, rank)
    except Exception:
        pass
    _threshold["at"] = now
    _threshold["rank"] = rank
    return rank


class LogBus:
    def __init__(self, capacity: int = 3000):
        self._buf: collections.deque = collections.deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, msg: str, level: str = "info", source: str = "app") -> dict:
        if level not in LEVELS:
            level = "info"
        with self._lock:
            self._seq += 1
            rec = {
                "seq": self._seq,
                "ts": round(time.time(), 3),
                "level": level,
                "source": source,
                "msg": str(msg),
            }
            self._buf.append(rec)
            return rec

    def since(self, seq: int, limit: int = 500) -> list:
        with self._lock:
            out = [r for r in self._buf if r["seq"] > seq]
        return out[-limit:]

    def tail(self, n: int = 200) -> list:
        with self._lock:
            return list(self._buf)[-n:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


LOG = LogBus()


def log(msg: str, level: str = "info", source: str = "app") -> dict:
    lvl = level if level in _RANK else "info"
    if _RANK[lvl] < _threshold_rank():
        # 低于配置级别：不进缓冲、也不打 stdout（否则面板 90% 是访问日志噪音）
        return {"seq": 0, "ts": round(time.time(), 3), "level": lvl,
                "source": source, "msg": str(msg), "filtered": True}
    rec = LOG.add(msg, lvl, source)
    # 同时打到 stdout：`ha addons logs` 能看到，便于没有面板时排查
    print(f"[{rec['level']}] [{rec['source']}] {msg}", flush=True)
    return rec
