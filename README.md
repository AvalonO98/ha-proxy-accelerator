# Net Proxy · HA 网络代理插件

给 Home Assistant（HAOS / Supervised）用的网络代理与镜像加速 Add-on：

- **可交互界面**：Supervisor 原生选项 + 侧边栏 Ingress 面板，带总开关；
- **三种代理方式**：Docker 加速镜像 / 上游 HTTP-SOCKS5 代理与订阅 / 内置 WireGuard 出口；
- **真正作用于 HA 系统更新与插件安装**：直接改 Docker 守护进程配置，让 `ghcr.io`、`docker.io`
  的镜像拉取走代理或加速源。

## 为什么必须用预构建镜像分发

目标环境（中国大陆常见网络）里 **Docker Hub 不可达**。而 Supervisor 在本地构建 add-on 时，
需要先从 Docker Hub 拉取构建器镜像（`docker:<版本>-cli`）：

```
Pulling image docker:29.7.2-cli
Can't pull image docker:29.7.2-cli: Get "https://registry-1.docker.io/v2/": timeout
```

也就是说：**在 Docker Hub 被墙的 HA 上，本地构建的插件永远装不上**（先有鸡还是先有蛋——
这个插件本身正是用来修镜像源的）。因此本插件以 **ghcr.io 预构建镜像** 分发：

- CI（GitHub Actions）构建多架构镜像并推送到 `ghcr.io`；
- Supervisor 只做 `pull`，而 `ghcr.io` 在目标环境可直连；
- 插件装好后，它自己会把 `registry-mirrors` 配好，之后的本地构建也随之可用。

## 安装

1. Home Assistant → **设置 → 加载项 → 加载项商店 → 右上角 ⋮ → 仓库**，添加：
   `https://github.com/AvalonO98/ha-proxy-accelerator`
   （若 GitHub 直连不稳定，可用加速前缀：`https://ghfast.top/https://github.com/AvalonO98/ha-proxy-accelerator`）
2. 刷新商店，安装 **Net Proxy (网络代理)**。
3. 启动后点侧边栏「网络代理」打开面板。
4. 在面板里：选择代理方式 → 填镜像源/上游 → 打开总开关（或直接点「应用并生效」）。

> 插件需要 `host_network` + `host_pid` + `privileged: [SYS_ADMIN, ...]`，因为要读写宿主机的
> `/etc/docker/daemon.json` 并让 dockerd 重新加载。安装时 Home Assistant 会给出权限提示，属预期。

## 三种代理方式

| 方式 | 做什么 | 生效范围 | 生效代价 |
|---|---|---|---|
| **A. Docker 加速镜像** | 改写 `daemon.json` 的 `registry-mirrors` | 仅 Docker Hub | **SIGHUP 热加载，不中断 HA** |
| **B. 上游代理 / 订阅** | 内置 mihomo 提供本地 `127.0.0.1:7890`，dockerd 的 `proxies` 指向它 | `ghcr.io` + Docker Hub（所有 registry） | 需重启一次 dockerd（HA 短暂中断） |
| **C. 内置 WireGuard 出口** | 粘贴 `.conf`，mihomo 用 userspace WireGuard 作为出口 | 同 B | 同 B |

**Docker 与 GitHub 两类镜像源都可以自定义**：

- Docker 加速镜像：任意增删 `registry-mirrors` 条目；
- GitHub 加速源：任意前缀（`https://ghfast.top/`）或 `{url}` 模板，插件下载内核/访问 GitHub
  时按顺序自动回退。

## 安全设计（这是重点）

- **只动自己管理的两个键**：`registry-mirrors` 与 `proxies`，其余键（`log-driver`、`data-root`、
  `bip`…）原样保留；
- **写前备份 + JSON 校验 + 原子替换**：绝不产生半截文件；
- **反向校验**：写完用 dockerd 的实时配置（Docker Engine API `/info`）核对，不一致立即回滚；
- **重启 dockerd 交给宿主独立 systemd 单元**执行，并在脚本里内建「起不来就还原备份」；
- **重启后自校验**：插件随 dockerd 一起重启，回来后自动确认；失败则回滚并关闭总开关；
- **看门狗**：方式 B/C 下持续用「经代理访问 ghcr.io」做健康检查，持续失败按阈值自动回滚；
- **自锁防护**：`127.0.0.0/8`、`172.30.32.0/23`（HA 内部网络）、内网段、`.local.hass.io` 全部直连，
  避免把 Supervisor ↔ 容器通信塞进代理。

## 目录结构

```
netproxy/            # add-on（config.yaml / Dockerfile / run.sh / app / static / translations）
tests/               # 纯标准库测试（wg 解析、daemon.json 安全逻辑、mihomo 配置生成、镜像源回退）
.github/workflows/   # 构建并推送 ghcr 多架构镜像
repository.yaml      # HA add-on 仓库描述
```

## 测试

```bash
python tests/run_tests.py     # 无需 pytest / 第三方依赖
```

## 免责声明

本插件只做网络加速/代理，用于改善镜像拉取速度与可达性，不绕过任何付费或许可限制。
修改 Docker 守护进程配置存在风险，请在使用前确认你已了解「方式 B/C 会重启 dockerd」这一点；
插件内建了备份与自动回滚，但**重要环境请先做 HA 快照**。
