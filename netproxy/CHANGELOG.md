# 变更记录

## 0.1.0

首个版本。

**界面**
- Ingress 侧边栏面板：总开关、三种代理方式卡片、真实生效状态、连通性检测、实测拉取镜像、
  运行日志、变更历史与备份一键回滚；中英双语。
- Supervisor 原生选项 + 中英文 translations（含嵌套对象、password 字段）。

**代理方式**
- A：Docker 加速镜像（`registry-mirrors`，SIGHUP 热加载，不中断 HA）；
- B：上游 HTTP/SOCKS5 代理 / Clash-mihomo 订阅（内置 mihomo，dockerd `proxies`）；
- C：内置 WireGuard 出口（`.conf` → mihomo userspace wireguard 出站，不改主机路由）。

**镜像源**
- Docker 与 GitHub 两类镜像源**均可自定义**：Docker 写入 `registry-mirrors`；
  GitHub 加速前缀（含 `{url}` 模板）用于插件下载内核/访问 GitHub，按顺序自动回退。

**安全**
- 只托管 `registry-mirrors` / `proxies` 两个键，其余键原样保留；
- JSON 校验 + 原子替换 + 应用前备份 + 用 dockerd 实时配置反向校验；
- 重启 dockerd 交由宿主独立 systemd 单元执行，脚本内建失败还原；
- 重启后自校验、看门狗自动回滚、`no-proxy` 自锁防护（HA 内部网段强制直连）。

**分发**
- 以 ghcr.io 预构建多架构镜像分发（GitHub Actions），规避"Docker Hub 被墙导致
  Supervisor 本地构建必然失败"的引导问题。
