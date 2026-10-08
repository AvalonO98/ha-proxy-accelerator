"""镜像源（Docker 自定义 / GitHub 自定义加速前缀）测试。"""
from __future__ import annotations

from netproxy_app import mihomo

ASSET = "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.32/mihomo-linux-amd64-compatible-v1.19.32.gz"


def test_apply_mirrors_prefix_form():
    got = mihomo.apply_mirrors(ASSET, ["https://ghfast.top/"])
    assert got == [("https://ghfast.top/" + ASSET, "https://ghfast.top/")]


def test_apply_mirrors_adds_scheme_and_handles_extra_slash():
    assert mihomo.apply_mirrors(ASSET, ["ghproxy.net"])[0][0] == "https://ghproxy.net/" + ASSET
    assert mihomo.apply_mirrors(ASSET, ["https://x.com//"])[0][0] == "https://x.com/" + ASSET


def test_apply_mirrors_template_form():
    tpl = "https://mirror.example/proxy?url={url}"
    got = mihomo.apply_mirrors(ASSET, [tpl])
    assert got == [("https://mirror.example/proxy?url=" + ASSET, tpl)]


def test_apply_mirrors_ignores_blank_and_supports_custom_order():
    assert mihomo.apply_mirrors(ASSET, []) == []
    assert mihomo.apply_mirrors(ASSET, ["", "   ", None]) == []
    got = mihomo.apply_mirrors(ASSET, ["https://b/", "https://a/"])
    assert [g[1] for g in got] == ["https://b/", "https://a/"]   # 顺序即优先级


def test_official_asset_url_with_explicit_version():
    ok, tag, url, err = mihomo.official_asset_url("1.19.32")
    assert ok, err
    assert tag == "v1.19.32"
    assert url.startswith("https://github.com/MetaCubeX/mihomo/releases/download/v1.19.32/mihomo-linux-")
    assert url.endswith(".gz")


def test_official_asset_url_keeps_v_prefix():
    ok, tag, _, _ = mihomo.official_asset_url("v1.19.32")
    assert ok and tag == "v1.19.32"


def test_candidate_urls_order_and_content():
    custom = "https://my.mirror/mihomo.gz"
    cands = mihomo.candidate_urls(custom, ["https://ghfast.top/", "https://gh-proxy.com/"], "v1.19.32")
    labels = [c[0] for c in cands]
    urls = [c[1] for c in cands]
    assert labels[0] == "自定义地址" and urls[0] == custom
    assert any(l.startswith("GitHub 直连") for l in labels)
    assert sum(1 for l in labels if l.startswith("加速源")) == 2
    # 自定义地址之后才是 GitHub 直连，再之后是加速源
    assert labels[1].startswith("GitHub 直连")
    assert urls[-1].startswith("https://gh-proxy.com/")


def test_candidate_urls_no_version_never_raises():
    # 不给版本号时会尝试 GitHub API；无论网络通不通都不能抛异常
    cands = mihomo.candidate_urls("", [], "")
    assert isinstance(cands, list)
    for label, url in cands:
        assert isinstance(label, str) and isinstance(url, str)


def test_asset_names_match_arch_rules():
    names = mihomo._asset_names("v1.19.32")
    assert names and all(n.endswith(".gz") for n in names)
    assert all("v1.19.32" in n for n in names)


def test_download_reports_error_and_leaves_no_partial_file():
    # 不依赖外网：故意连一个必然拒绝的本地端口
    import os
    from netproxy_app import settings
    dest = os.path.join(str(settings.BIN_DIR), "mihomo-selftest")
    ok, msg = mihomo._download("http://127.0.0.1:9/nope.gz", dest)
    assert ok is False and msg
    assert not os.path.exists(dest)
    assert not os.path.exists(dest + ".part")
