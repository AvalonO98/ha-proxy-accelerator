"""测试引导：必须在导入任何 netproxy 模块之前设置环境变量。

settings/hostops 在 **import 时** 读取环境变量，所以这里要最早执行：
  * NETPROXY_DATA       → 指向临时目录，避免污染真实 /data
  * NETPROXY_HOST_ROOT  → 假根目录，让"宿主机文件读写"可以在本机测试
并把 netproxy/app 以包名 netproxy_app 挂到 sys.modules 上（与容器内
`python3 -m netproxy_app.main` 的布局一致，否则相对导入无法解析）。

临时目录按 环境变量 → 仓库上级目录/.tmp-test → 系统临时目录 的顺序挑选，
第一个真正可写的位置胜出（本机沙箱下不同的位置可写性不同）。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "netproxy" / "app"


def _pick_tmp() -> str:
    cands = []
    if os.environ.get("NETPROXY_TEST_TMP"):
        cands.append(os.environ["NETPROXY_TEST_TMP"])
    cands.append(str(ROOT.parent / ".tmp-test" / "netproxy-tests"))
    cands.append(os.path.join(tempfile.gettempdir(), "netproxy-tests"))
    errors = []
    for c in cands:
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, ".write-probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("x")
            os.remove(probe)
            return c
        except Exception as e:  # 换下一个候选
            errors.append(f"{c}: {type(e).__name__}: {e}")
    raise RuntimeError("没有可写的临时目录：" + " ｜ ".join(errors))


TMP_DIR = _pick_tmp()
os.environ["NETPROXY_DATA"] = os.path.join(TMP_DIR, "data")
os.environ["NETPROXY_HOST_ROOT"] = os.path.join(TMP_DIR, "hostroot")

os.makedirs(os.path.join(os.environ["NETPROXY_DATA"], "netproxy", "backups"), exist_ok=True)
os.makedirs(os.path.join(os.environ["NETPROXY_HOST_ROOT"], "etc", "docker"), exist_ok=True)

FAKE_ETC_DOCKER = os.path.join(os.environ["NETPROXY_HOST_ROOT"], "etc", "docker")


def install_package() -> None:
    if "netproxy_app" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "netproxy_app", APP_DIR / "__init__.py",
        submodule_search_locations=[str(APP_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["netproxy_app"] = mod
    spec.loader.exec_module(mod)


install_package()
