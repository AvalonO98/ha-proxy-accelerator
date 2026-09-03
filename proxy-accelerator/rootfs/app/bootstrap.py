#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次启动引导: 把 Supervisor 的 /data/options.json 合并进 /data/settings.json。
仅当 settings.json 尚不存在, 或 options 里字段值非空时覆盖。"""
import json
import os

DATA = os.environ.get("DATA_DIR", "/data")
OPTIONS = os.path.join(DATA, "options.json")
STATE = os.path.join(DATA, "settings.json")

KEYMAP = {
    "enabled": "enabled",
    "mode": "mode",
    "upstream": "upstream",
    "upstream_user": "upstream_user",
    "upstream_pass": "upstream_pass",
    "acl_mode": "acl_mode",
    "hacs_patch_enabled": "hacs_patch_enabled",
    "hacs_github_proxy": "hacs_github_proxy",
}


def main():
    try:
        with open(OPTIONS, "r", encoding="utf-8") as f:
            opts = json.load(f)
    except Exception:
        opts = {}

    try:
        with open(STATE, "r", encoding="utf-8") as f:
            settings = json.load(f)
        exists = True
    except Exception:
        settings = {}
        exists = False

    changed = False
    for opt_key, state_key in KEYMAP.items():
        if opt_key in opts and opts[opt_key] is not None:
            # 已存在的手动配置优先(仅在没有该键或值为空时引导)
            if not exists or state_key not in settings or \
                    settings[state_key] in (None, "", False, []) and \
                    state_key not in ("enabled", "hacs_patch_enabled", "acl_mode"):
                settings[state_key] = opts[opt_key]
                changed = True

    if changed or not exists:
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE)
        print("[bootstrap] settings written ->", STATE)


if __name__ == "__main__":
    main()
