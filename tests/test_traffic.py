"""流量统计的记账逻辑。

这里守的是"不会算错"的几条：
  * 首次快照只当基线，不能把连接开始之前的历史字节算进来；
  * 之后每次只累加差值（mihomo / docker 给的都是累计值）；
  * 计数器倒退（连接/容器重建）不能产生负增量；
  * 消失的连接丢掉基线，避免 id 复用时把新旧流量串起来；
  * 目标要能分到 mirror / registry / other，页面靠它分组。
"""
from __future__ import annotations

from netproxy_app import traffic


def test_delta_meter_first_snapshot_is_baseline():
    m = traffic.DeltaMeter()
    out = m.feed({"c1": (500, 5000)})
    assert out == [("c1", 0, 0, True)]


def test_delta_meter_accumulates_only_deltas():
    m = traffic.DeltaMeter()
    m.feed({"c1": (100, 1000)})
    out = m.feed({"c1": (300, 1500), "c2": (7, 7)})
    got = {k: (u, d) for k, u, d, _ in out}
    assert got["c1"] == (200, 500)
    assert got["c2"] == (0, 0)          # 新连接：只记基线


def test_delta_meter_counter_reset_is_not_negative():
    m = traffic.DeltaMeter()
    m.feed({"c1": (1000, 9000)})
    out = m.feed({"c1": (10, 20)})      # 计数器归零重建
    assert out == []                    # 不产生负增量，也不重复计
    out = m.feed({"c1": (60, 120)})     # 重建后的真实增量照常记
    got = {k: (u, d) for k, u, d, _ in out}
    assert got["c1"] == (50, 100)


def test_delta_meter_drops_vanished_ids():
    m = traffic.DeltaMeter()
    m.feed({"c1": (100, 100), "c2": (100, 100)})
    m.feed({"c1": (200, 200)})
    # c2 消失后再以同样的 id 出现，应被视为新基线（不会把旧值算进来）
    out = m.feed({"c2": (999, 999)})
    assert out == [("c2", 0, 0, True)]


def test_classify_host():
    cfg = {"mirrors": ["https://docker.m.daocloud.io", "https://docker.1ms.run"]}
    assert traffic.classify_host("docker.m.daocloud.io", cfg) == "mirror"
    assert traffic.classify_host("DOCKER.1MS.RUN", cfg) == "mirror"
    assert traffic.classify_host("ghcr.io", cfg) == "registry"
    assert traffic.classify_host("registry-1.docker.io", cfg) == "registry"
    assert traffic.classify_host("raw.githubusercontent.com", cfg) == "other"
    assert traffic.classify_host("", cfg) == "other"


def test_app_and_chain_names():
    assert traffic._app_name({"process": "dockerd"}) == "dockerd"
    assert traffic._app_name({"processPath": "/usr/bin/curl"}) == "curl"
    assert traffic._app_name({"sourceIP": "192.0.2.9"}).startswith("未知")
    assert traffic._chain_name({"chains": ["PROXY", "香港 01"]}) == "PROXY → 香港 01"
    assert traffic._chain_name({"chains": []}) == "未知"


def test_meter_aggregates_and_summary():
    m = traffic.TrafficMeter()          # 只构造，不 start()，避免起线程
    m._session.clear()
    m._add(("dockerd", "docker.m.daocloud.io", "mirror", "DIRECT"), 100, 900, True)
    m._add(("dockerd", "docker.m.daocloud.io", "mirror", "DIRECT"), 50, 200, False)
    m._add(("python3", "ghcr.io", "registry", "PROXY → 香港 01"), 10, 40, True)

    s = m.summary("session")
    assert s["total"]["up"] == 160 and s["total"]["down"] == 1140
    assert s["apps"][0]["name"] == "dockerd"          # 按总量降序
    assert s["apps"][0]["down"] == 1100
    targets = {t["name"]: t for t in s["targets"]}
    assert targets["docker.m.daocloud.io"]["kind"] == "mirror"
    assert targets["ghcr.io"]["kind"] == "registry"
    chains = {c["name"]: c for c in s["chains"]}
    assert chains["DIRECT"]["down"] == 1100
    assert chains["PROXY → 香港 01"]["up"] == 10


def test_meter_reset_keeps_daily():
    m = traffic.TrafficMeter()
    m._session.clear()
    m._add(("x", "y", "other", "DIRECT"), 1, 2, True)
    assert m.summary("session")["total"]["down"] == 2
    m.reset()
    assert m.summary("session")["total"]["down"] == 0
    assert m.summary("today")["total"]["down"] >= 2     # 日均值不受清零影响
