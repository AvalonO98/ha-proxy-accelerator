#!/usr/bin/env sh
set -e

# 首次启动: 用 /data/options.json(Supervisor 传入的插件配置) 初始化 /data/settings.json
python3 /app/bootstrap.py || true

exec python3 /app/server.py \
  --proxy-port "${PROXY_PORT:-8899}" \
  --web-port "${WEB_PORT:-8099}"
