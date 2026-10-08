"""内存日志环形缓冲，供面板轮询。

不落盘：日志量小、面板是唯一消费者；落盘反而会在 /data 里堆垃圾。
"""
from __future__ import annotations

import collections
import threading
import time

LEVELS = ("trace", "debug", "info", "notice", "warning", "error", "fatal")


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
    rec = LOG.add(msg, level, source)
    # 同时打到 stdout：`ha addons logs` 能看到，便于没有面板时排查
    print(f"[{rec['level']}] [{rec['source']}] {msg}", flush=True)
    return rec
