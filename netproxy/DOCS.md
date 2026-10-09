# Net Proxy 使用文档

本文档对应 add-on 的「文档」页。面板本身（侧边栏「网络代理」）已经把所有操作做成了图形界面，
这里补充原理、选项细节与排错。

---

## 1. 它到底改了系统的什么

Home Assistant 的系统更新、插件（Add-on）安装，本质都是 **Supervisor 让宿主机的 Docker 守护进程
（dockerd）去拉取镜像**：

```
Supervisor ──► dockerd ──► ghcr.io / registry-1.docker.io
```

所以要让它们走代理/加速源，唯一有效的着力点就是 dockerd 的配置
`/etc/docker/daemon.json` 里的两个键：

| 键 | 作用 | Home Assistant 更新 | 插件安装 | 生效方式 |
|---|---|---|---|---|
| `registry-mirrors` | Docker Hub 加速镜像 | 仅在镜像来自 Docker Hub 时 | 同左 | **SIGHUP 热加载，不中断** |
| `proxies` | HTTP(S) 代理 | ✅ 覆盖 `ghcr.io` 与 Docker Hub | ✅ | 必须重启 dockerd |

> 关键点：HA 的镜像主要来自 **ghcr.io**，而 `registry-mirrors` 只对 Docker Hub 生效。
> 想让 ghcr.io 也走代理，必须用 **方式 B/C**（`proxies`）。

插件只增删这两个键，其余键（`log-driver`、`data-root`、`bip`、`storage-driver`…）原样保留。

---

## 2. 三种代理方式

### 方式 A：Docker 加速镜像（默认，最安全）

- 只写 `registry-mirrors`，用 **SIGHUP** 让 dockerd 热加载，**不重启、不中断 HA**。
- 默认内置 `https://docker.m.daocloud.io`、`https://docker.1ms.run`，可在面板「镜像源」里
  任意增删自定义条目（每行一个，必须带 `http(s)://`）。
- 局限：只加速 Docker Hub；`ghcr.io`（HA 系统更新/多数官方插件）不受影响。

适用：你只想加速 Docker Hub 的拉取，且希望零中断。

### 方式 B：上游 HTTP/SOCKS5 代理 / 订阅

> ## ⚠️ 使用方式 B/C 前必读（真实事故复盘）
>
> 方式 B/C 需要**重启 dockerd**（`proxies` 不支持热加载）。在 HAOS 上，重启 dockerd 会把
> **Supervisor 容器一起杀掉**；实测某些情况下 Supervisor 被自动拉起后**启动流程会中途失败**
> （只走到 `Phase 'initialize'`），结果 **Core 与所有插件都起不来**，只能重启宿主恢复。
>
> 更糟的是：如果插件此时又被拉起（例如 Supervisor 的应用看门狗），它若"自动重放"带重启的配置，
> 就会再次重启 dockerd → 形成每 ~15 分钟一轮的**死循环**。0.1.9 及更早版本正是这样把一台 HA
> 打挂了 4.5 小时。
>
> 因此从 **0.1.10** 起：
> 1. **开机流程永不自动重启 dockerd**：方式 B/C 只提示，必须由你在面板点「应用并生效」；
> 2. 重启后校验失败也**不再自动重启** dockerd，只回滚文件并关闭总开关；
> 3. 宿主脚本在手动重启 dockerd 后会**显式重启 Supervisor**（让启动流程从头完整走一遍），
>    尽量把 Core 与各应用带回来。
>
> **建议**：应用方式 B/C 前先做 HA 快照；尽量在你在场、能使用 ESXi/物理控制台的时候操作；
> 若应用后 HA 长时间不回来，直接在控制台执行 `ha host reboot`（这是确定有效的恢复手段）。

- 插件内置 **mihomo** 内核，在 `127.0.0.1:7890` 提供本地 HTTP+SOCKS5 混合入口；
- 把 dockerd 的 `proxies` 指向它 → **所有 registry 都走代理**（含 ghcr.io）；
- 上游可以填：
  - `http://192.168.1.10:7890`、`socks5://192.168.1.10:1080`
  - 带认证：面板里分别填用户名/密码；或直接写进 URL（`socks5://user:pass@host:1080`），
    两种写法插件都会识别；
  - Clash/mihomo 订阅链接（内核以 proxy-provider 方式拉取）；
  - **订阅被墙时的退路**：把订阅内容保存为 `/share/netproxy/subscription.yaml`，内核会自动
    改用这个本地文件（面板上也会有提示）。
- 代价：首次应用需要**重启一次 dockerd**（所有容器含 HA 核心一起重启，30–60 秒恢复）。

### 方式 C：内置 WireGuard 出口

- 面板里粘贴标准 WireGuard `.conf`（`[Interface]` + `[Peer]`），插件解析后生成 mihomo 的
  **userspace wireguard 出站**。
- 为什么不用 `wg-quick`/内核模块：那会建立 tun 并改宿主机路由表，一旦配错会切断
  Supervisor ↔ 容器之间 `172.30.32.0/24` 的通信（HA 直接失联）。userspace 出站只影响
  经本地代理出去的流量，风险面小得多。
- 注意：mihomo 的 wireguard 出站只支持单个 Peer，多 Peer 时只用第一个。
- 代价：同方式 B，需要重启一次 dockerd。

> OpenVPN 目前**未支持**（mihomo 无 OpenVPN 出站）。如果你只有 OpenVPN，可把它跑在别的机器上，
> 用方式 B 指向那台机器的 HTTP/SOCKS5 代理。

---

## 3. 界面说明

- **总开关**：打开/关闭代理链路。关闭时会把两个托管键还原成插件介入之前的样子（精确还原，
  包括"原本不存在"的情况）。状态徽标显示的是**真实生效状态**，来自 dockerd 实时配置。
- **镜像源**：Docker 与 GitHub 两类都可以自定义：
  - Docker 列表 → 写入 `registry-mirrors`；
  - GitHub 加速源 → 插件下载 mihomo 内核、访问 GitHub 时使用的加速前缀，支持
    `https://ghfast.top/`（会拼成 `https://ghfast.top/https://github.com/...`）或含 `{url}`
    的模板，按顺序尝试并自动回退。
- **应用并生效**：把当前表单落盘并应用到系统；方式 B/C 会二次确认（会重启 dockerd）。
- **回滚到应用前**：一键恢复 daemon.json，并关闭总开关。
- **检测连通性**：并列显示「直连 / 走代理」对 ghcr.io、Docker Hub 的结果，以及各加速镜像源、
  各 GitHub 加速源的可达性与耗时——用来判断"到底是代理没生效，还是目标本来就不通"。
- **实测拉取镜像**：直接调用 Docker API 让 dockerd 真的拉一个镜像（端到端验证）。
  需要 docker socket 可写；只读时面板会如实提示"无法实测"。
- **变更历史 / 备份**：每次变更都有记录，可对任意一次备份一键回滚。

---

## 4. 配置项（Supervisor 原生选项）

| 选项 | 说明 |
|---|---|
| `enabled` | 总开关 |
| `mode` | `docker_mirror` / `upstream_proxy` / `wireguard` |
| `mirrors` | Docker Hub 加速镜像（可自定义，每行一个） |
| `github_mirrors` | GitHub 加速前缀（可自定义，按顺序回退） |
| `upstream.type` / `url` / `username` / `password` | 上游代理 |
| `subscription.url` / `name` | Clash/mihomo 订阅 |
| `wireguard.config` | WireGuard `.conf` 全文 |
| `kernel_url` | 自定义内核下载地址（留空 = 自动选源） |
| `kernel_version` | 指定 mihomo 版本（GitHub API 不可达时用） |
| `dns` | 内核解析用 DNS |
| `auto_rollback` | 代理不健康时自动回滚并关闭 |
| `health_check_seconds` | 健康检查容忍时长（默认 300 秒） |
| `apply_timeout_seconds` | 重启 dockerd 后等待恢复的最长时间（默认 180 秒） |
| `log_level` | 日志级别 |

面板里改的值保存在 `/data`，与原生选项互不覆盖（面板优先）。

---

## 5. 安全机制（写入 dockerd 配置为什么不会把 HA 搞挂）

1. **备份**：每次应用前把 daemon.json 原文（含"原本是否存在"）备份到 `/data/netproxy/backups`；
2. **校验**：组装后先做 JSON + 键类型校验，非法内容直接拒绝，不写盘；
3. **原子替换**：写临时文件再 `mv`，不会出现半截 JSON；
4. **反向校验**：写完后读 dockerd 实时配置（`/info` 的 `RegistryConfig.Mirrors`、`HttpProxy`、
   `HttpsProxy`）核对，不一致立刻还原并重新加载/重启；
5. **重启交给宿主独立单元**：重启 dockerd 会杀掉插件自己，所以这一步用宿主上的
   `systemd-run --unit=netproxy-apply` 执行，脚本内建"起不来就还原备份再重启"；
6. **重启后自校验**：插件随 dockerd 重启回来后，自动核对配置；失败则回滚并关闭总开关；
7. **看门狗**：方式 B/C 下持续用「经代理访问 `https://ghcr.io/v2/`」做健康检查，
   持续失败超过阈值 → 自动回滚 + 关闭总开关；
8. **自锁防护**：本地/内网/HA 内部网段（`172.30.32.0/23`）在 `no-proxy` 里强制直连。

---

## 6. 排错

### 面板显示「拿不到宿主机文件系统访问权限」

**原因：Supervisor 的「保护模式」还开着。** 这不是插件能自己声明的设置——
`protected` 只存在于 Supervisor 的用户数据 schema（`SCHEMA_APP_USER`），写在 `config.yaml`
里会被忽略；而保护模式下 Supervisor 不会把 `host_pid` 与 `docker_api` 应用到容器
（`supervisor/docker/app.py`：`return "host" if not self.app.protected and self.app.host_pid else None`），
于是插件拿不到宿主 PID 命名空间，读写宿主 `/etc/docker/daemon.json` 全部失效。

两种修法，任选其一：

1. **一键修复（推荐）**：面板上红色横幅里点「一键修复权限（关闭保护模式并重启插件）」。
   插件会调用 Supervisor 的 `POST /addons/self/security {"protected": false}`，
   再 `POST /addons/self/restart` 重启自己使新参数生效。
2. **手动**：设置 → 加载项 → Net Proxy → 关闭「保护模式」→ 重启插件。

重启后面板首页的「宿主访问能力」应显示 `host:host`，且不再有红色横幅。

> 为什么服务进程不是 s6？
> Supervisor 创建 add-on 容器时**硬编码** `entrypoint=["/init"]`
> （`supervisor/docker/cli.py`），所以镜像里的 ENTRYPOINT/CMD 声明无效；容器永远
> 执行基础镜像的 `/init`，而 HA 官方基础镜像的 `/init` 是 s6-overlay，
> `s6-overlay-suexec` **只允许 PID 1 运行**。本插件声明了 `host_pid`（共享宿主 PID
> 命名空间）后不再是 PID 1，s6 会直接以退出码 100 崩溃（实机日志：
> `s6-overlay-suexec: fatal: can only run as pid 1`）。
> 因此本插件的 Dockerfile 把镜像里的 `/init` 替换成了自己的入口脚本（exec 传入的
> `/run.sh`），彻底绕开 s6 —— 插件不需要 s6/bashio。

插件需要的完整权限如下（`config.yaml` 已声明）：

```yaml
host_network: true         # 与宿主共享网络，本地代理监听 127.0.0.1 供 dockerd 使用
host_pid: true             # 与宿主共享 PID 命名空间 → 可发 SIGHUP、可直接 nsenter
apparmor: false            # AppArmor 会拦截 setns(进入宿主命名空间)
privileged: [SYS_ADMIN, SYS_PTRACE, NET_ADMIN, NET_RAW, DAC_READ_SEARCH]
                           # SYS_ADMIN  → nsenter/setns 进入宿主 mount 命名空间
                           # SYS_PTRACE → 打开 /proc/1/ns/mnt 需要 ptrace 权限
                           #              (HAOS 内核默认 YAMA ptrace_scope 会拦，缺它必失败：
                           #               nsenter: can't open '/proc/1/ns/mnt': Permission denied)
docker_api: true           # 读 dockerd 实时配置(/info)，用于"真实生效"校验
map: [share:rw]            # /share 用于手动放置 mihomo 内核与本地订阅文件
# 另需：保护模式 = 关闭（见上，插件无法自行声明，但可在面板上一键关闭）
```

> 安全提醒：这些权限等价于宿主机 root（本插件本来就要改 dockerd 配置）。
> 请只在自己的 HA 上使用，不要把它暴露到公网。面板本身走 Home Assistant 的 Ingress 鉴权，
> 默认不发布任何端口。

### HAOS 的 /etc 是只读的（插件怎么落地配置）

HAOS 的 rootfs 不可变，`/etc/docker/daemon.json` 是 OS 提供的只读文件，直接写会报
`Read-only file system`。因此插件分两级落地：

1. 先尝试**直接原子写**（适用于 `/etc` 可写的环境，例如 Supervised on Debian）；
2. 只读时，把完整内容写到可写的持久位置，再用 **`mount --bind`** 覆盖
   `/etc/docker/daemon.json`：

   | 优先级 | 落地点 | 说明 |
   |---|---|---|
   | 1 | `/etc/udev/rules.d/netproxy-daemon.json` | HAOS 官方支持持久化 udev 规则的位置 |
   | 2 | `/mnt/overlay/etc/docker/daemon.json` | hassos-overlay 持久层 |
   | 3 | `/mnt/data/netproxy/daemon.json` | 数据分区，必然可写 |
   | 4 | `/run/netproxy-daemon.json` | tmpfs，仅作兜底（重启即失） |

**回滚就是 `umount`** —— HAOS 自带的不可变文件重新露出来，所以无论怎么折腾，
OS 原始配置都不会被破坏。

bind mount 不跨重启，因此插件启动时会**自动重新落地**（写文件是幂等的；
并且用内核 `boot_id` 做闸门，同一次开机内最多触发一次"需要重启 dockerd"的落地，
不会出现重启循环）。重启宿主后如果你希望镜像源立刻生效，等插件起来即可（几十秒内）。

### 方式 B/C 应用后 HA 没回来
- 插件会在 3 分钟后由宿主脚本自动还原配置并重启 docker；
- 若仍然不行：用 Samba 或 File editor 打开 `/config`，手动把 `daemon.json` 里的 `proxies` 段删掉；
- 极端情况下进入 HAOS 主机控制台（或 Advanced SSH 插件）删除/修正 `/etc/docker/daemon.json`
  后 `systemctl restart docker`。

### 明明开了代理，`ha core update` 还是慢
`proxies` 只对 **dockerd 拉镜像**生效。HA 内核自身的其它 HTTP 请求（如 HACS、集成下载）
不经过 dockerd，需要用其它方式处理。另外确认面板「真实生效状态」里 `HttpProxy`/`HttpsProxy`
确实显示了地址——那才是 dockerd 的真实状态。

### 想彻底卸载恢复
1. 面板点「回滚到应用前」（或关掉总开关应用一次）；
2. 卸载插件；
3. 宿主机 `/etc/docker/daemon.json` 已恢复原样；`/data/netproxy` 会随插件卸载删除。

---

## 7. 手动引导（本地构建不可用时）

若你的 HA 上 Docker Hub 不可达，Supervisor 本地构建插件会失败：

```
Pulling image docker:<ver>-cli → registry-1.docker.io timeout
```

此时有两种办法：

1. **用预构建镜像**（本插件默认方式）：CI 构建推送到 ghcr.io，Supervisor 只 pull，无需 Docker Hub；
2. **先手工把镜像源配上**（一次性），之后本地构建即可用。在 HAOS 主机控制台执行：

```sh
# 备份
cp /etc/docker/daemon.json /etc/docker/daemon.json.bak 2>/dev/null || true
# 写入加速镜像（保留其它键：这里假设原本没有该文件；已有内容请自行合并）
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run"
  ]
}
EOF
systemctl restart docker     # 或 kill -HUP $(pidof dockerd) 仅重载镜像源
```

> ⚠️ 上面的写法会覆盖已有 daemon.json，**已有内容时必须手工合并**，否则会丢掉
> `data-root`、`bip` 等关键配置，可能导致 dockerd 起不来。

之后本插件（方式 A）就可以直接在面板里管理这些镜像源了。
