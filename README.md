# Net Proxy · HA 网络代理插件

给 Home Assistant（HAOS / Supervised）用的网络代理与镜像加速 Add-on：

- **可交互界面**：Supervisor 原生选项 + 侧边栏 Ingress 面板，带总开关；
- **三种代理方式**：Docker 加速镜像 / 上游 HTTP-SOCKS5 代理与订阅 / 内置 WireGuard 出口；
- **真正作用于 HA 系统更新与插件安装**：直接改 Docker 守护进程配置，让 `ghcr.io`、`docker.io`
  的镜像拉取走代理或加速源；
- **流量统计页**：按应用（进程/容器）、目标（镜像源/registry）、出口（直连/代理节点）
  累计上下行字节。方式 B/C 走本机内核，归因完整；方式 A 是直连，只能给容器级总量。

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
3. **关闭「保护模式」并保持关闭**（见下一节，这是硬性前提）。
4. 启动后点侧边栏「网络代理」打开面板。
5. 在面板里：选择代理方式 → 填镜像源/上游 → 打开总开关（或直接点「应用并生效」）。

> 插件需要 `host_network` + `host_pid` + `privileged: [SYS_ADMIN, ...]`，因为要读写宿主机的
> `/etc/docker/daemon.json` 并让 dockerd 重新加载。安装时 Home Assistant 会给出权限提示，属预期。

## 必须长期关闭「保护模式」（重要）

这不是"安装时关一次就好"的步骤，而是**只要想让插件干活就必须一直关着**。

Supervisor 在 `supervisor/docker/app.py` 里把三个能力都挂在同一个前置条件上：

```python
@property
def pid_mode(self):                       # 共享宿主 PID 命名空间
    if not self.app.protected and self.app.host_pid:
        return "host"
    return None

# Docker API support
if not self.app.protected and self.app.access_docker_api:
    mounts.append(MOUNT_DOCKER)           # 挂载 /var/run/docker.sock

if not self.app.protected and self.app.with_full_access:
    ...                                   # full_access / privileged
```

于是 `protected: true` 时：

| 插件需要的能力 | 保护模式开启时 | 后果 |
|---|---|---|
| `host_pid` → 进宿主 mount 命名空间读写 `/etc/docker/daemon.json` | 不生效 | 方式 A/B/C 全部无效 |
| `docker_api` → 挂 docker.sock 读 `/info` 做"真实生效"校验 | 不挂载 | 无法校验、无法实测拉取 |
| `with_full_access` → `privileged` 生效 | 不生效 | 缺 `SYS_ADMIN`，`nsenter` 失败 |

此时面板顶部会显示「拿不到宿主机文件系统访问权限」，**并且不会写入任何配置**——
插件会直接拒绝执行，不会留下半套 daemon.json。

### 怎么关、怎么确认

- 设置 → 应用（旧版本叫「加载项」）→ **Net Proxy** → 关闭「保护模式」→ 重启该应用；
- **只能手动关**：插件自己调 `POST /addons/self/security` 会返回 **403**（该端点要求管理员级
  令牌，普通 add-on token 与 Terminal&SSH 的 manager token 都不行）。同理「看门狗/自动恢复」
  对应的 `POST /apps/<slug>/options` 也是 403 —— 面板只能提示，不能替你打开；
- 确认：`ha apps info <slug> | grep protected`，或看面板顶部有没有红色横幅。

另外建议把插件页面的「**自动恢复**」（旧名「看门狗」）也打开：它关着的时候，插件进程崩溃
不会被 Supervisor 拉起，会一直停在 `error` 状态。

### 这是不是降低了安全性

是，这是一个明确的取舍：关掉保护模式后，该容器能读写宿主的 Docker 配置、能看到宿主进程。
插件在这个前提下做的收敛是：

- **只动自己管理的两个键**（`registry-mirrors` / `proxies`），其余键（`log-driver`、`data-root`、
  `bip`、`ipv6`…）原样保留；
- 写入前先备份、JSON 校验、原子替换；写后用 dockerd 实时配置反向校验，不一致立即回滚；
- 宿主命令走白名单（`hostops._ALLOWED_BIN`），不拼接执行任意命令；
- 面板需经 HA ingress（带登录态）访问；手动发布端口时必须带一次性 token。

如果你不能接受这个取舍，这个插件就不适合你的环境——它没有"不开保护模式也能改 dockerd"
的实现路径。

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

这些都建立在「保护模式已关闭」这个前提上 —— 它的原因、风险与取舍，见上面的
**必须长期关闭「保护模式」** 一节。

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

## 许可证

[MIT](LICENSE) © 2026 HA Proxy Accelerator Team。

可自由使用、修改、再分发（含商用），只需保留版权声明与许可声明；软件按"现状"提供，不附带担保。

## 参考与依赖

**仓库里没有第三方代码文件**：`netproxy/app` 的后端只用 Python 标准库（`import` 全来自
stdlib，没有 `requirements.txt`），测试也不依赖 pytest。构建期与运行时用到的东西如下：

| 组件 | 关系 | 许可证 |
|---|---|---|
| [mihomo (Clash.Meta)](https://github.com/MetaCubeX/mihomo) | **可选**内核，由插件在运行时下载到 `/data`，不随镜像分发；以独立进程 + 本地端口调用，不与本项目链接 | MIT |
| Home Assistant Supervisor | 运行平台：apps API / ingress 面板 / `host_pid` / `docker_api` | Apache-2.0 |
| Docker Engine API | 通过 `/var/run/docker.sock` 调 HTTP 接口，不依赖 docker CLI | Apache-2.0 |
| `ghcr.io/home-assistant/*-base-python` | 镜像基础层 | Apache-2.0（含 Alpine 等组件各自许可） |
| `actions/checkout`、`docker/*-action` | CI 构建与推送 | MIT / Apache-2.0 |
| Docker Hub 镜像源（daocloud、1ms.run 等） | 用户在面板里自行增删的**公共服务**，本项目不打包、不代理其内容 | 各自服务条款 |

以上 SPDX 标识均取自各仓库的 `LICENSE`（可用 GitHub API `/repos/<owner>/<repo>` 的
`license.spdx_id` 复核）。它们都是宽松许可，与本项目的 MIT 兼容。

顺带说明**为什么内核不在镜像里**：mihomo 是独立进程、通过本地端口调用，本身也不是链接关系；
放到运行时下载主要是为了让用户能自选版本、走自己的 GitHub 加速源，以及不必每次改内核都重建镜像
——不是许可上的限制。

**设计上的参考（不含代码搬运）**：

- HAOS 上「`/etc` 只读 → 把真配置放到可写位置，再 bind mount 覆盖到 `/etc/docker/daemon.json`」
  的社区通行做法；
- Supervisor 的 apps 模型（ingress 面板、`host_pid`、`docker_api`、watchdog 的触发条件）；
- Docker 官方文档里 `registry-mirrors` / `proxies` 的语义，以及哪些配置项支持 SIGHUP 热加载。

排错文档里引用了 Supervisor 源码片段（`supervisor/docker/app.py`、`supervisor/apps/app.py`）
作为**说明性引用**，用于解释"保护模式/看门狗为什么这样表现"，未复制进本项目。

## 免责声明

本插件只做网络加速/代理，用于改善镜像拉取速度与可达性，不绕过任何付费或许可限制。
修改 Docker 守护进程配置存在风险，请在使用前确认你已了解「方式 B/C 会重启 dockerd」这一点；
插件内建了备份与自动回滚，但**重要环境请先做 HA 快照**。
