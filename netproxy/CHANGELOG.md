# 变更记录

## 0.1.4

**修复：宿主访问探测的真实误判（实机定位）**
- 实机现象：`protected: false` + `host_pid` + `docker_api` 全部生效（`pid1=systemd`、
  能看到 `dockerd` PID、能读 dockerd 实时配置），但插件仍报告 `host_access: false`，
  于是 apply 直接以「当前模式：local」失败。
- 根因：探测用 `/proc/1/root/etc/os-release` 判断"PID 1 的根文件系统是不是宿主"。
  但读取别的进程的 `/proc/PID/root` 需要 **ptrace 权限**，容器里没有 `SYS_PTRACE`
  时返回 `EACCES`，于是被判成"没有共享 PID 命名空间"。
- 改为**功能性验证**：执行 `nsenter -t 1 -m -- cat /etc/os-release`，把它与容器自己的
  `/etc/os-release` 比较 —— 不同就说明确实进入了宿主根文件系统（另附宿主标记文件兜底）。
  这条判据同时覆盖了"保护模式导致 host_pid 未生效"和"nsenter 失败"两种情况，
  并把原因如实写进 `capability.reason` 供面板显示。

## 0.1.3

**修复（0.1.2 的修法被证明无效，这次是从 Supervisor 源码定位）**
- Supervisor 创建 add-on 容器时**硬编码** `entrypoint=["/init"]`
  （`supervisor/docker/cli.py`），因此镜像里的 `ENTRYPOINT`/`CMD` 声明改不了入口：
  容器永远执行基础镜像的 `/init`，即 s6-overlay。s6 只在 PID 1 可运行，`host_pid`
  启用后必然崩溃（`s6-overlay-suexec: fatal: can only run as pid 1`，退出码 100）。
  现在 Dockerfile 直接把镜像里的 `/init` 替换成自己的入口脚本（exec 传入的
  `/run.sh`），彻底绕开 s6。
- 0.1.2 的 `ENTRYPOINT []` 保留（对本地 `docker run` 自洽），但注释已更正为事实。

## 0.1.2

实机验证（HAOS + x86_64，关闭保护模式后）继续修复：

**修复**
- **容器启动即崩（退出码 100）**：HA 官方基础镜像的 ENTRYPOINT 是 s6-overlay 的 `/init`，
  而 `s6-overlay-suexec` 只允许 PID 1 运行；启用 `host_pid` 后进程不再是 PID 1，
  实测日志 `s6-overlay-suexec: fatal: can only run as pid 1` → 插件无法启动。
  Dockerfile 现在显式 `ENTRYPOINT []`，由 `/run.sh` 直接作为主进程（插件不需要 s6/bashio）。
- **保护模式无法由插件声明**：核对 Supervisor 源码后确认 `protected` 只存在于
  `SCHEMA_APP_USER`（用户持久化），写在 `config.yaml` 里会被忽略；而保护模式开启时
  `host_pid` / `docker_api` 均不生效（`supervisor/docker/app.py`），插件彻底不可用。
  新增 **一键修复**：`POST /addons/self/security {"protected": false}` +
  `POST /addons/self/restart`，面板红色横幅上直接可点（也可在插件页面手动关闭后重启）。

**诊断**
- `/api/diag` 增加 `supervisor_self`（来自 Supervisor 的 self 信息），便于一眼看出
  保护模式/host_pid/docker_api 是否真的生效。

## 0.1.1

真实 HAOS（HAOS + x86_64，Docker Hub 不可达）实机验证后修复：

**修复**
- **关闭保护模式**：`supervisor/docker/app.py` 中 `host_pid` 与 `docker_api` 只在
  `not protected` 时生效（默认 `protected: true`），实测会导致插件拿不到宿主 PID
  命名空间、没有 docker socket，从而完全无法改 dockerd 配置。现在由
  `config.yaml` 声明 `protected: false`，用户无需手工关开关。
- **修正宿主访问误判**（实机复现）：原来只判断 `nsenter -t 1 -m` 是否成功，
  保护模式下 PID 1 是容器自己的 init，`nsenter` 会"成功"进入**本容器**的 mount
  namespace（`/etc/os-release` 仍是 Alpine），于是误判为有宿主访问、写入却落在容器内。
  现在改为校验 PID 1 的根文件系统确实是宿主（存在 `/etc/hassos-release`、
  `/usr/lib/systemd/systemd` 等标记），并在面板上给出明确原因。
- **translations 结构**：嵌套配置组的翻译必须含字符串型 `name`（组显示名），与"名为
  `name` 的选项"冲突。`subscription.name` 选项改名为 `subscription.profile`，并补齐
  `upstream` / `subscription` / `wireguard` 的组名（中英）。

**文档**
- DOCS 增加保护模式说明与完整权限清单；README 说明"Docker Hub 被墙时 Supervisor
  无法本地构建 add-on"的引导问题与预构建镜像方案。

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
