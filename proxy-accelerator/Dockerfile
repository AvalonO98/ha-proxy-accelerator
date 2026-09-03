# 基于 HA 官方多架构基础镜像构建, 无第三方 Python 依赖
ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8
ENV S6_SERVICES_READYTIME 2000
# stdout/stderr 实时可见(add-on 日志面板/容器日志)
ENV PYTHONUNBUFFERED 1
ENV PYTHONIOENCODING utf-8

WORKDIR /app

# base 镜像已含 python3; 此处兜底确保存在
RUN apk add --no-cache python3 || true

COPY rootfs/run.sh /run.sh
COPY rootfs/app /app

RUN chmod a+x /run.sh

ENTRYPOINT ["/run.sh"]
