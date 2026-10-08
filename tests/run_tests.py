"""无 pytest 依赖的测试运行器：`python tests/run_tests.py`。

设计目标：在只有 Python 标准库的环境里也能跑（本机沙箱内 pip 不可用）。
收集各 test_*.py 里的 test_* 函数依次执行，打印 PASS/FAIL 与回溯。
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _bootstrap  # noqa: E402  必须最早执行

MODULES = [
    "test_wg",
    "test_settings",
    "test_daemonconf",
    "test_mihomo_config",
    "test_mirrors",
]


def main() -> int:
    failures = []
    total = 0
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:
            print(f"[加载失败] {name}\n{traceback.format_exc()}")
            failures.append((name, "import"))
            continue
        print(f"\n=== {name} ===")
        for fname in sorted(dir(mod)):
            if not fname.startswith("test_"):
                continue
            fn = getattr(mod, fname)
            if not callable(fn):
                continue
            total += 1
            try:
                fn()
                print(f"  PASS  {fname}")
            except Exception as e:
                failures.append((f"{name}.{fname}", traceback.format_exc()))
                print(f"  FAIL  {fname}: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    if failures:
        print(f"结果：{total - len(failures)}/{total} 通过，{len(failures)} 个失败\n")
        for name, tb in failures:
            print(f"--- {name} ---\n{tb}")
        return 1
    print(f"结果：{total}/{total} 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
