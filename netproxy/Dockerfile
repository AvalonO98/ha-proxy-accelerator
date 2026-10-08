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

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/api/health',timeout=4).status==200 else 1)"
CMD ["/run.sh"]
