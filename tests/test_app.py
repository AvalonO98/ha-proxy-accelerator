"""静态资源解析 + 内核安装回归 + 日志级别过滤 + 版本一致性。

这几条都是"看起来能用、实际是坏的"，因此各留一个断言：
  * 静态路径必须挡住 `..` 与"同前缀兄弟目录"；
  * 「安装/更新内核」曾在下载参数里引用后赋值的 cfg → UnboundLocalError；
  * `log_level` 曾经只影响 mihomo，插件自己的 debug 访问日志照旧灌满面板；
  * 版本号在 app / config.yaml / CI 三处必须一致（否则面板显示旧版本、镜像拉不到）。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import tempfile

from netproxy_app import logbus, main
from netproxy_app.version import VERSION

ROOT = pathlib.Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 静态资源
# --------------------------------------------------------------------------- #
def test_static_serves_real_files():
    js = main.static_file("/app.js")
    assert js and os.path.isfile(js) and js.endswith("app.js")
    idx = main.static_file("/")
    assert idx and idx.endswith("index.html")


def test_static_unknown_path_falls_back_to_index():
    # 面板是单页应用，未知路径回到 index.html 属于预期
    fallback = main.static_file("/no-such-file.css")
    assert fallback and fallback.endswith("index.html")


def test_static_rejects_traversal():
    base = tempfile.mkdtemp(prefix="netproxy-static-")
    try:
        root = os.path.join(base, "static")
        sibling = os.path.join(base, "staticX")     # 同前缀兄弟目录
        os.makedirs(root)
        os.makedirs(sibling)
        with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
            f.write("ok")
        with open(os.path.join(sibling, "secret.txt"), "w", encoding="utf-8") as f:
            f.write("secret")

        # startswith 判断会放过它，commonpath 不会
        got = main.static_file("/../staticX/secret.txt", root=root)
        assert got and got.endswith("index.html"), got
        got = main.static_file("/../staticX/secret.txt", root=root)
        assert os.path.basename(got) != "secret.txt"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 内核安装回归
# --------------------------------------------------------------------------- #
def test_kernel_install_passes_configured_mirrors():
    seen = {}
    origin_install = main.mihomo.install

    def fake_install(explicit_url="", mirrors=None, version="", progress=None):
        seen.update(url=explicit_url, mirrors=mirrors, version=version)
        return False, "所有下载源都失败（测试替身）"

    main.mihomo.install = fake_install
    try:
        try:
            main._do_kernel_install("https://example.com/mihomo.gz", None)
        except RuntimeError as e:
            assert "所有下载源都失败" in str(e)
        else:  # pragma: no cover
            raise AssertionError("install 失败时必须抛 RuntimeError")
    finally:
        main.mihomo.install = origin_install

    # 0.1.12 在这里会抛 UnboundLocalError（cfg 未赋值就先用了）
    assert seen["url"] == "https://example.com/mihomo.gz"
    assert isinstance(seen["mirrors"], list)


def test_kernel_install_success_reports_version():
    origin_install = main.mihomo.install
    origin_ensure = main.modes.ensure_kernel_running
    main.mihomo.install = lambda **kw: (True, "v9.9.9（来源：测试）")
    main.modes.ensure_kernel_running = lambda cfg: (True, "内核已启动")
    try:
        res = main._do_kernel_install("", None)
    finally:
        main.mihomo.install = origin_install
        main.modes.ensure_kernel_running = origin_ensure
    assert res["state"] == "done" and "v9.9.9" in res["version"]


# --------------------------------------------------------------------------- #
# 日志级别
# --------------------------------------------------------------------------- #
def test_debug_is_filtered_at_info_level():
    logbus._threshold["at"] = 0.0                       # 强制按当前配置重判
    logbus._threshold["rank"] = logbus._RANK["info"]
    before = len(logbus.LOG.tail(500))
    rec = logbus.log("access log line", "debug", "http")
    assert rec.get("filtered") is True
    assert len(logbus.LOG.tail(500)) == before


def test_warning_is_kept():
    logbus._threshold["at"] = 0.0
    logbus._threshold["rank"] = logbus._RANK["info"]
    rec = logbus.log("something odd", "warning", "test")
    assert not rec.get("filtered")
    assert any(r["msg"] == "something odd" for r in logbus.LOG.tail(50))


# --------------------------------------------------------------------------- #
# 版本号单一来源
# --------------------------------------------------------------------------- #
def test_version_matches_addon_config_and_ci():
    cfg = (ROOT / "netproxy" / "config.yaml").read_text(encoding="utf-8")
    m = re.search(r'^version:\s*"([^"]+)"', cfg, re.M)
    assert m, "config.yaml 里找不到 version"
    assert m.group(1) == VERSION, f"config.yaml={m.group(1)} 与 app/version.py={VERSION} 不一致"

    wf = (ROOT / ".github" / "workflows" / "build-netproxy.yml").read_text(encoding="utf-8")
    m = re.search(r'VERSION:\s*"([^"]+)"', wf)
    assert m, "workflow 里找不到 VERSION"
    assert m.group(1) == VERSION, f"CI={m.group(1)} 与 app/version.py={VERSION} 不一致"
