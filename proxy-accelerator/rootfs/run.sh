#!/usr/bin/env sh
set -e

echo "[run.sh] container start, python: $(command -v python3 || echo MISSING)"

# 首次启动: 用 /data/options.json(Supervisor 传入的插件配置) 初始化 /data/settings.json
python3 /app/bootstrap.py || echo "[run.sh] bootstrap failed (non-fatal)"

echo "[run.sh] launching server.py ..."

exec python3 /app/server.py \
  --proxy-port "${PROXY_PORT:-8899}" \
  --web-port "${WEB_PORT:-8099}"
