"""流量统计：按「应用 / 目标 / 出口」累计字节数。

数据来源有两条，且它们能看到的东西**本质不同**，页面必须分开标注：

1. **mihomo 控制接口 `/connections`**（方式 B/C）
   本机所有走代理的流量都从这里过，每条连接带 `metadata.process`（进程名）、
   `metadata.host`（目标域名）、`chains`（命中 DIRECT 还是某个节点/策略组）以及
   该连接**累计**的 upload/download。这是唯一能回答"走的哪个镜像/代理"的来源。

2. **Docker Engine API 容器统计**（任何方式都可用）
   `GET /containers/<id>/stats` 给每个容器 `networks.*.rx_bytes/tx_bytes`（自容器启动
   累计）。它只能回答"哪个应用/容器用了多少"，**没有镜像/代理归属**——方式 A 下
   dockerd 直连镜像源，链路上根本没有记录点。

两条来源的单位都是"累计值"，所以这里统一用**差分**记账：每次采集只累加
`本次值 - 上次值`，连接/容器消失时丢弃基线。这样既不会重复计数，也不会因为
重启丢总量（日均值另外落盘）。

持久化只存按天的聚合（`/data/netproxy/traffic.json`，保留 7 天），桶的基数上限
有硬性裁剪，避免长期运行把内存/磁盘拖垮。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

from . import dockerapi, mihomo, settings
from .logbus import log

TRAFFIC_FILE = "traffic.json"

#: mihomo 连接状态的采集间隔（每次只是一次本地 HTTP，很轻）
CONN_INTERVAL = 10.0
#: 容器统计的采集间隔（每个容器一次 API，12 个容器并行也要 1~2 秒，别太勤）
DOCKER_INTERVAL = 60.0
#: 落盘间隔
SAVE_INTERVAL = 60.0
#: 平均保留天数
KEEP_DAYS = 7
#: 单个窗口里最多保留多少个桶（超了丢最久未更新的）
MAX_KEYS = 800

AGENT_HOSTS = ("ghcr.io", "registry-1.docker.io", "docker.io", "index.docker.io",
               "auth.docker.io", "production.cloudflare.docker.com")


# --------------------------------------------------------------------------- #
# 差分记账（纯逻辑，可单测）
# --------------------------------------------------------------------------- #
class DeltaMeter:
    """把"累计值快照"换算成增量。

    调用方每次喂一份 {id: (up, down)}；返回 [(id, d_up, d_down, is_new)]。
    * 首次出现的 id 记基线、不算增量（它的历史值发生在统计开始之前）；
    * 数值倒退（进程/连接重建）视为新基线，不产生负增量；
    * 本次快照里消失的 id 直接丢基线。
    """

    def __init__(self) -> None:
        self._seen: dict = {}

    def feed(self, snapshot: dict) -> list:
        out = []
        for key, (up, down) in snapshot.items():
            prev = self._seen.get(key)
            if prev is None:
                self._seen[key] = (up, down)
                out.append((key, 0, 0, True))
                continue
            d_up = up - prev[0]
            d_down = down - prev[1]
            if d_up < 0 or d_down < 0:
                # 计数器归零/重建：重新记基线
                d_up = d_down = 0
            self._seen[key] = (up, down)
            if d_up or d_down:
                out.append((key, d_up, d_down, False))
        for gone in [k for k in self._seen if k not in snapshot]:
            self._seen.pop(gone, None)
        return out

    def reset(self) -> None:
        self._seen.clear()


# --------------------------------------------------------------------------- #
# 桶
# --------------------------------------------------------------------------- #
def _bucket() -> dict:
    return {"up": 0, "down": 0, "conns": 0, "last": 0.0}


def classify_host(host: str, cfg: dict) -> str:
    """把目标归到 mirror / registry / other，页面用它分组显示。"""
    h = (host or "").lower()
    if not h:
        return "other"
    for m in cfg.get("mirrors") or []:
        try:
            from urllib.parse import urlparse
            mh = (urlparse(str(m)).hostname or "").lower()
        except Exception:
            mh = ""
        if mh and mh == h:
            return "mirror"
    if any(h == a or h.endswith("." + a) for a in AGENT_HOSTS):
        return "registry"
    return "other"


def _app_name(meta: dict) -> str:
    for k in ("process", "processPath"):
        v = str(meta.get(k) or "").strip()
        if v:
            return os.path.basename(v) if k == "processPath" else v
    ip = str(meta.get("sourceIP") or "").strip()
    return f"未知({ip})" if ip else "未知"


def _chain_name(conn: dict) -> str:
    chains = conn.get("chains") or []
    if not chains:
        return "未知"
    return " → ".join(str(c) for c in chains)


# --------------------------------------------------------------------------- #
# 统计器
# --------------------------------------------------------------------------- #
class TrafficMeter(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="traffic", daemon=True)
        self._lock = threading.RLock()
        self._conn_meter = DeltaMeter()
        self._docker_meter = DeltaMeter()
        self.session_start = time.time()
        #: {(app, target, kind, chain): bucket}
        self._session: dict = {}
        #: {日期: {key: bucket}}
        self._daily: dict = {}
        self._last_save = 0.0
        self._last_docker = 0.0
        self._last_note = ""
        self._load()

    # ---------------- 持久化 ----------------
    def _path(self) -> str:
        return str(settings.STATE_DIR / TRAFFIC_FILE)

    def _load(self) -> None:
        try:
            with open(self._path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("daily"), dict):
                self._daily = data["daily"]
        except Exception:
            self._daily = {}

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < SAVE_INTERVAL:
            return
        self._last_save = now
        cutoff = time.strftime("%Y-%m-%d", time.localtime(now - KEEP_DAYS * 86400))
        with self._lock:
            self._daily = {d: v for d, v in self._daily.items() if d >= cutoff}
            payload = {"daily": self._daily, "saved": now}
        try:
            settings.ensure_dirs()
            tmp = self._path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self._path())
        except Exception as e:
            log(f"流量统计落盘失败：{type(e).__name__}: {e}", "warning", "traffic")

    # ---------------- 记账 ----------------
    def _add(self, key: tuple, up: int, down: int, new_conn: bool) -> None:
        today = time.strftime("%Y-%m-%d")
        now = time.time()
        with self._lock:
            for store in (self._session, self._daily.setdefault(today, {})):
                b = store.get(key)
                if b is None:
                    if len(store) >= MAX_KEYS:
                        # 丢掉最久没更新的桶，防止基数爆炸
                        oldest = min(store, key=lambda k: store[k].get("last", 0))
                        store.pop(oldest, None)
                    b = store[key] = _bucket()
                b["up"] += up
                b["down"] += down
                b["last"] = now
                if new_conn:
                    b["conns"] += 1

    def _collect_mihomo(self) -> None:
        if not mihomo.alive():
            self._conn_meter.reset()   # 内核重启后连接 id 全变，旧基线作废
            return
        data = mihomo.ctrl_get("/connections", timeout=6.0)
        if not isinstance(data, dict) or "_error" in data:
            return
        conns = data.get("connections") or []
        snapshot = {}
        meta_by_id = {}
        for c in conns:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            snapshot[cid] = (int(c.get("upload") or 0), int(c.get("download") or 0))
            meta_by_id[cid] = c
        for cid, d_up, d_down, is_new in self._conn_meter.feed(snapshot):
            c = meta_by_id.get(cid) or {}
            meta = c.get("metadata") or {}
            app = _app_name(meta)
            target = str(meta.get("host") or meta.get("destinationIP") or "").strip() or "未知"
            cfg = settings.load_config()
            kind = classify_host(target, cfg)
            self._add((app, target, kind, _chain_name(c)), d_up, d_down, is_new)

    def _collect_docker(self) -> None:
        now = time.time()
        if now - self._last_docker < DOCKER_INTERVAL:
            return
        self._last_docker = now
        if not dockerapi.summarize().get("available"):
            return
        status, data = dockerapi._request("GET", "/containers/json", timeout=15)
        if status != 200 or not isinstance(data, list):
            return
        snapshot = {}
        for c in data[:40]:
            cid = str(c.get("Id") or "")
            if not cid:
                continue
            name = (c.get("Names") or ["?"])[0].lstrip("/")
            st, stats = dockerapi._request(
                "GET", f"/containers/{cid}/stats?stream=false&one-shot=true", timeout=12)
            if st != 200 or not isinstance(stats, dict):
                continue
            rx = tx = 0
            for nic in (stats.get("networks") or {}).values():
                rx += int(nic.get("rx_bytes") or 0)
                tx += int(nic.get("tx_bytes") or 0)
            snapshot[name] = (tx, rx)          # (上行, 下行)
        for name, d_up, d_down, is_new in self._docker_meter.feed(snapshot):
            self._add((name, "全部目标", "container", "Docker 直连"), d_up, d_down, is_new)

    # ---------------- 主循环 ----------------
    def run(self) -> None:
        last_conn = 0.0
        while True:
            try:
                if time.time() - last_conn >= CONN_INTERVAL:
                    last_conn = time.time()
                    self._collect_mihomo()
                self._collect_docker()
                self._save()
            except Exception as e:      # pragma: no cover - 兜底
                log(f"流量统计异常：{type(e).__name__}: {e}", "error", "traffic")
            time.sleep(5)

    # ---------------- 查询 ----------------
    def _rows(self, store: dict) -> dict:
        apps, targets, chains, total = {}, {}, {}, {"up": 0, "down": 0, "conns": 0}
        for (app, target, kind, chain), b in store.items():
            for name, d in ((app, apps), (target, targets), (chain, chains)):
                cur = d.setdefault(name, {"up": 0, "down": 0, "conns": 0, "kind": kind})
                cur["up"] += b["up"]
                cur["down"] += b["down"]
                cur["conns"] += b["conns"]
            total["up"] += b["up"]
            total["down"] += b["down"]
            total["conns"] += b["conns"]
        return {"apps": apps, "targets": targets, "chains": chains, "total": total}

    @staticmethod
    def _top(d: dict, n: int = 30) -> list:
        rows = [{"name": k, **v, "bytes": v["up"] + v["down"]} for k, v in d.items()]
        rows.sort(key=lambda r: r["bytes"], reverse=True)
        return rows[:n]

    def summary(self, window: str = "session") -> dict:
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            if window == "today":
                store = self._daily.get(today, {})
            else:
                store = self._session
            agg = self._rows(store)
            days = sorted(self._daily.keys())[-KEEP_DAYS:]
            daily_total = {d: sum(b["up"] + b["down"] for b in self._daily.get(d, {}).values())
                           for d in days}
        cfg = settings.load_config()
        mode = cfg.get("mode")
        kernel = mihomo.alive()
        return {
            "window": window,
            "session_start": self.session_start,
            "since": self.session_start if window != "today" else time.time(),
            "total": agg["total"],
            "apps": self._top(agg["apps"]),
            "targets": self._top(agg["targets"]),
            "chains": self._top(agg["chains"]),
            "daily_total": daily_total,
            "sources": {
                "mihomo": kernel,
                "docker": bool(dockerapi.summarize().get("available")),
                "mode": mode,
            },
            "note": ("方式 A：dockerd 直连镜像源，链路不经本机代理，只有容器级总量、"
                     "无法归因到具体镜像源；切到方式 B/C 才能看到「走的哪个代理」。"
                     if mode == "docker_mirror" else ""),
        }

    def reset(self) -> None:
        with self._lock:
            self._session.clear()
        self._conn_meter.reset()
        self._docker_meter.reset()
        self.session_start = time.time()
        log("流量统计已清零（仅本次会话，日均值保留）", "notice", "traffic")


METER: Optional[TrafficMeter] = None


def start() -> TrafficMeter:
    global METER
    if METER is None:
        METER = TrafficMeter()
        METER.start()
    return METER
