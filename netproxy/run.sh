#!/usr/bin/env bash
# Net Proxy add-on 入口。init: false 时本脚本即容器 PID 1，必须 exec 到主进程。
set -eu

DATA_DIR="${NETPROXY_DATA:-/data}"
mkdir -p "${DATA_DIR}/netproxy/bin" "${DATA_DIR}/netproxy/backups" "${DATA_DIR}/netproxy/run" 2>/dev/null || true

echo "[netproxy] start; data=${DATA_DIR}; pid1=$(cat /proc/1/comm 2>/dev/null)"

# Supervisor 把 config.yaml 的 options 渲染到这里；不在 Supervisor 下运行时允许缺失。
if [ -f "${DATA_DIR}/options.json" ]; then
    echo "[netproxy] options.json found"
else
    echo "[netproxy] WARN: ${DATA_DIR}/options.json not found (running outside Supervisor?)"
fi

cd /app
exec python3 -m netproxy_app.main
