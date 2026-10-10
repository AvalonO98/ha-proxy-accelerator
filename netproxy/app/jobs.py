"""异步任务：应用配置要重启 dockerd，耗时可能超过 ingress 的 HTTP 超时，
所以所有会改宿主状态的操作用后台线程跑，面板轮询任务状态。
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Callable, Optional

from .logbus import LOG, log

_JOBS: dict = {}
_lock = threading.RLock()
_mutating = threading.Lock()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def current() -> Optional[dict]:
    with _lock:
        running = [j for j in _JOBS.values() if j["state"] == "running"]
        if running:
            return sorted(running, key=lambda j: j["started"])[-1]
        if not _JOBS:
            return None
        return sorted(_JOBS.values(), key=lambda j: j["started"])[-1]


def get(job_id: str) -> Optional[dict]:
    with _lock:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def list_jobs(limit: int = 10) -> list:
    with _lock:
        return [dict(j) for j in sorted(_JOBS.values(), key=lambda x: x["started"], reverse=True)[:limit]]


def busy() -> bool:
    with _lock:
        return any(j["state"] == "running" for j in _JOBS.values())


def submit(kind: str, fn: Callable[[dict], dict], mutating: bool = True) -> dict:
    """提交任务。

    并发规则：写操作（mutating）必须独占；只读任务（连通性检测、实测拉取等）
    可以与其它只读任务并行，但不能与写操作并行（否则会观察到半落地的状态）。
    """
    with _lock:
        running = [j for j in _JOBS.values() if j["state"] == "running"]
        if mutating and running:
            return {"error": "已有任务在执行中，请等它结束"}
        if not mutating and any(j.get("mutating") for j in running):
            return {"error": "有写操作任务在执行中，请等它结束"}
    seq_start = LOG.tail(1)[0]["seq"] if LOG.tail(1) else 0
    job = {
        "id": _new_id(),
        "kind": kind,
        "mutating": bool(mutating),
        "state": "running",
        "started": time.time(),
        "finished": None,
        "result": None,
        "error": None,
        "log_from": seq_start,
    }
    with _lock:
        _JOBS[job["id"]] = job

    def _run():
        log(f"任务开始：{kind} ({job['id']})", "notice", "job")
        acquired = False
        try:
            if mutating:
                acquired = _mutating.acquire(timeout=600)
                if not acquired:
                    raise RuntimeError("等待其它任务超时")
            res = fn(job)
            job["result"] = res
            job["state"] = "done"
            log(f"任务完成：{kind} ({job['id']})", "notice", "job")
        except Exception as e:
            job["state"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            log(f"任务失败：{kind} → {job['error']}", "error", "job")
            log(traceback.format_exc(limit=4), "debug", "job")
        finally:
            if acquired:
                _mutating.release()
            job["finished"] = time.time()

    threading.Thread(target=_run, name=f"job-{kind}", daemon=True).start()
    return dict(job)


def job_log(job: dict, limit: int = 300) -> list:
    if not job:
        return []
    return [r for r in LOG.tail(limit) if r["seq"] > job.get("log_from", 0)]
