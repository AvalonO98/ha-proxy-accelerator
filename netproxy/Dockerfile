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

# ⚠️ 必须清空基础镜像的 ENTRYPOINT。
# HA 官方基础镜像的 ENTRYPOINT 是 s6-overlay 的 /init，而 s6-overlay-suexec 只在
# **PID 1** 时才能运行；本插件声明了 host_pid（共享宿主 PID 命名空间），容器进程
# 不再是 PID 1，于是 s6 会直接以退出码 100 崩掉（实机日志：
# "s6-overlay-suexec: fatal: can only run as pid 1"）。
# 本插件不需要 s6/bashio，直接让 /run.sh 作为主进程即可。
ENTRYPOINT []

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/api/health',timeout=4).status==200 else 1)"
CMD ["/run.sh"]
