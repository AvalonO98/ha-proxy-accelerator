"""pytest 兼容入口（本机没装 pytest，但装了的话也能直接 `pytest tests`）。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402
