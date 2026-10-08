ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.18
FROM ${BUILD_FROM}

# 后端只用 Python 标准库，内核(nsenter/kill/sh)全部来自基础镜像自带的
# busybox + python3，因此这里不做任何 apk add：构建过程不依赖 Alpine CDN，
# 也不会因为软件源被墙而失败。
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /app
COPY app/ /app/netproxy_app/
COPY static/ /app/static/
COPY run.sh /run.sh

RUN chmod +x /run.sh \
 && python3 -m compileall -q /app/netproxy_app \
 && mkdir -p /opt/netproxy/bin

ENV NETPROXY_DATA=/data \
    NETPROXY_APP=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8

# ⚠️ 关键：Supervisor 创建 add-on 容器时**硬编码** `entrypoint=["/init"]`
#    （见 supervisor/docker/cli.py: `entrypoint=["/init"]`），所以镜像里写
#    ENTRYPOINT/CMD 都改不了入口 —— 容器永远执行基础镜像的 `/init`。
#    而 HA 官方基础镜像的 /init 是 s6-overlay，s6-overlay-suexec 只在 **PID 1**
#    时才能运行；本插件声明了 host_pid（共享宿主 PID 命名空间），容器进程不再是
#    PID 1，于是实测直接崩溃：
#        s6-overlay-suexec: fatal: can only run as pid 1   → 退出码 100
#    因此这里把 /init 替换成我们自己的入口脚本，彻底绕开 s6（插件不需要 s6/bashio）。
#    Supervisor 传入的命令是 `/init /run.sh`，我们的脚本会 exec 这些参数。
RUN rm -f /init \
 && printf '%s\n' '#!/bin/bash' 'set -e' \
      '# Net Proxy 入口：绕开 s6-overlay（host_pid 下它不是 PID 1 会崩）' \
      'if [ "$#" -gt 0 ]; then exec "$@"; fi' \
      'exec /run.sh' > /init \
 && chmod 0755 /init

# 对本地 `docker run` 也保持一致的入口（Supervisor 会覆盖它，这里是为了自洽）
ENTRYPOINT []

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/api/health',timeout=4).status==200 else 1)"
CMD ["/run.sh"]
