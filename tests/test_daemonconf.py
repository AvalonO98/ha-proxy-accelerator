"""daemon.json 组装/校验/备份/恢复测试——这是最高风险模块。"""
from __future__ import annotations

import json
import os

from netproxy_app import daemonconf, hostops


def _path() -> str:
    return hostops.hp(daemonconf.DAEMON_JSON)


def _write(content):
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if content is None:
        if os.path.exists(p):
            os.remove(p)
        return
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def test_read_missing_file():
    _write(None)
    cur = daemonconf.read_current()
    assert cur["existed"] is False and cur["ok"] is True
    assert cur["data"] == {} and cur["raw"] is None


def test_read_broken_json_is_reported_not_guessed():
    _write("{ this is not json")
    cur = daemonconf.read_current()
    assert cur["ok"] is False
    assert "JSON" in cur["error"]
    assert cur["data"] is None


def test_build_preserves_unmanaged_keys():
    cur = {"log-driver": "journald", "data-root": "/mnt/data/docker", "bip": "172.30.232.1/23"}
    plan = daemonconf.Plan(registry_mirrors=["https://m1", "https://m2"],
                           proxies={"http-proxy": "http://127.0.0.1:7890"})
    new = daemonconf.build(cur, plan)
    assert new["log-driver"] == "journald"
    assert new["data-root"] == "/mnt/data/docker"
    assert new["bip"] == "172.30.232.1/23"
    assert new["registry-mirrors"] == ["https://m1", "https://m2"]
    assert new["proxies"]["http-proxy"] == "http://127.0.0.1:7890"
    # 原始 dict 不能被就地修改
    assert "registry-mirrors" not in cur


def test_build_removes_managed_keys_when_empty():
    cur = {"registry-mirrors": ["https://x"], "proxies": {"http-proxy": "y"}, "bip": "z"}
    new = daemonconf.build(cur, daemonconf.Plan(registry_mirrors=[], proxies={}))
    assert "registry-mirrors" not in new and "proxies" not in new
    assert new["bip"] == "z"


def test_build_none_means_untouched():
    cur = {"registry-mirrors": ["keep"], "proxies": {"http-proxy": "keep"}}
    assert daemonconf.build(cur, daemonconf.Plan()) == cur


def test_validate_accepts_and_rejects():
    ok, _ = daemonconf.validate({"registry-mirrors": ["https://a"], "log-driver": "journald"})
    assert ok
    assert daemonconf.validate({"proxies": {"http-proxy": "http://127.0.0.1:7890",
                                           "no-proxy": "localhost,172.30.32.0/23"}})[0]
    bad_cases = [
        [],
        {"registry-mirrors": "https://a"},
        {"registry-mirrors": []},
        {"registry-mirrors": ["ftp://a"]},
        {"registry-mirrors": [123]},
        {"proxies": {"bogus-key": "x"}},
        {"proxies": {"http-proxy": 123}},
        {"proxies": {}},
    ]
    for case in bad_cases:
        ok, msg = daemonconf.validate(case)
        assert not ok, f"应当被拒绝：{case!r}（msg={msg}）"
        assert msg


def test_original_managed_snapshot():
    _write('{"log-driver":"journald","registry-mirrors":["https://orig"]}')
    cur = daemonconf.read_current()
    om = daemonconf.original_managed(cur["data"])
    assert om["registry-mirrors"] == {"present": True, "value": ["https://orig"]}
    assert om["proxies"] == {"present": False}


def test_backup_and_restore_roundtrip():
    _write('{"log-driver":"journald","registry-mirrors":["https://orig"]}')
    cur = daemonconf.read_current()
    bk = daemonconf.backup(cur, "unit-test")
    assert bk["existed"] is True and bk["raw"]

    _write('{"log-driver":"broken-by-someone-else"}')
    ok, msg = daemonconf.restore_raw(bk["raw"], bk["existed"])
    assert ok, msg
    assert json.loads(open(_path(), encoding="utf-8").read())["registry-mirrors"] == ["https://orig"]

    assert daemonconf.load_backup(bk["id"]) is not None
    assert any(b["id"] == bk["id"] for b in daemonconf.list_backups(20))


def test_restore_when_file_did_not_exist_deletes_it():
    _write(None)
    ok, msg = daemonconf.restore_raw('{"a":1}', False)
    assert ok, msg
    assert not os.path.exists(_path())


def test_write_is_validated_and_atomic():
    _write(None)
    obj = {"registry-mirrors": ["https://m"],
           "proxies": {"http-proxy": "http://127.0.0.1:7890", "no-proxy": "localhost"}}
    ok, msg = daemonconf.write_daemon_json(obj)
    assert ok, msg
    assert daemonconf.read_current()["data"] == obj

    # 非法内容必须被拒绝，且不破坏已有文件
    ok, msg = daemonconf.write_daemon_json({"registry-mirrors": ["not-a-url"]})
    assert not ok and msg
    assert daemonconf.read_current()["data"] == obj

    # 不留半截临时文件
    leftovers = [f for f in os.listdir(os.path.dirname(_path())) if ".netproxy." in f]
    assert not leftovers, leftovers


def test_diff_keys():
    assert daemonconf.diff_keys({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ["b"]
    assert daemonconf.diff_keys({"a": 1}, {"b": 2}) == ["a", "b"]
    assert daemonconf.diff_keys({}, {}) == []
    # proxies 变更必须被识别为"需要重启 dockerd"
    changed = daemonconf.diff_keys({"registry-mirrors": ["x"]},
                                   {"registry-mirrors": ["x"], "proxies": {"http-proxy": "y"}})
    assert changed == ["proxies"]
