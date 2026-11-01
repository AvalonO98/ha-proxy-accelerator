"""版本号单一来源。

版本号原本散落在三处，改一处漏两处就会导致"面板显示旧版本 / Supervisor 拉不到
镜像"这类难查问题（vX.Y.Z 与 image tag 必须一致）。现在：

    app/version.py  VERSION   ← 唯一权威值
    config.yaml     version   ← 由 tests/test_version.py 强制校验一致
    CI workflow     VERSION   ← 同上

/app 里没有 config.yaml（Dockerfile 只拷 app 与 static），所以运行时必须靠这个
常量，不能去读 add-on 配置。
"""
from __future__ import annotations

VERSION = "0.1.15"
