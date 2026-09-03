# HA 代理加速器（Proxy & Mirror Accelerator）

一个基于 **Home Assistant 加载项（Add-on）系统** 的「代理 + 加速」插件，把中国大陆场景下最常用的三种加速能力收进**一个带开关的可视化面板**：

| 你的需求 | 本插件实现 |
|---|---|
| ① 自选代理地址（HTTP/SOCKS5） | 面板填上游 `host:port` 即用；`direct`（本机直连）/ `http` / `socks5` 任选，一键切换 |
| ② Docker 镜像加速 | 内置常用 **Docker Hub** 加速镜像 + **可达性检测（延迟）**，一键生成宿主 `daemon.json` / HAOS 持久化 udev 脚本 |
| ③ GitHub 镜像加速（HACS 下载） | 把官方 HACS 的 GitHub 地址改写为**自选反代前缀**（ghfast / gh-proxy 等，可换），自动备份、可回滚、可检测被 HACS 自更新覆盖 |
| ④ 可视化面板、逐通道开关 | Ingress 网页面板；**每个开关即时生效**，状态持久化在 `/data` |
| ⑤ 加速 Home Assistant 内核更新 | Supervisor 更新镜像来自 ghcr.io —— 面板给出「dockerd 走代理」的宿主侧精确配置（Supervised / HAOS 两版），配合面板代理入口全链路生效 |

> 实现充分**借用了现有开源方案**的成熟思路：HACS 加速借鉴 [hacs-china / HACS 极速版](https://github.com/hacs-china/)（应用层替换 GitHub 地址）；HAOS 宿主侧持久化借鉴 [bbs.hassbian 名帖「udev 规则更换 docker 镜像源」](https://bbs.hassbian.com/forum.php?mod=viewthread&tid=26549)；镜像清单参考 [kubesre/docker-registry-mirrors](https://github.com/kubesre/docker-registry-mirrors) 与 [SeanChang/xuanyuan_docker_proxy](https://github.com/SeanChang/xuanyuan_docker_proxy)。详见文末「致谢与借鉴」。

---

## ⚠️ 先读：原理与安全边界（很重要）

1. **add-on 是沙箱容器，没有改宿主 Docker / 宿主 `/etc` 的受支持通道**（HAOS 根文件系统只读；Supervisor 也不提供该权限）。所以「让宿主 dockerd 走代理 / 换镜像源」这类操作，本插件负责**生成精确到可直接粘贴的配置 + 探测链路可用性**，最后由你手动应用一次。这不是偷懒，而是 Home Assistant 的设计边界——强行提权改宿主既不安全也会被 OS 更新冲掉。
2. **Docker 的 `registry-mirrors` 只对 Docker Hub 生效**，对 `ghcr.io`（HA 内核 / Supervisor / 官方 add-on 镜像所在）**无效**（参考 [官方讨论 #2797](https://github.com/home-assistant/operating-system/discussions/2797?sort=top)）。因此：
   - 面板 ② 的 Hub 加速：适用于 Docker Hub 来源的镜像；
   - 面板 ③ 的 ghcr 加速：必须让 **dockerd 走代理**（面板生成 systemd / udev 配置）。
3. **HACS 加速是应用层补丁**（改写 `custom_components/hacs` 里的下载地址），不是系统级代理。补丁前自动整目录备份；**HACS 自身更新会把补丁覆盖掉**，面板会检测到不一致并提示「重新应用」。
4. 公共镜像站、GitHub 反代均属第三方服务，**随时可能失效**——面板的检测按钮就是为这个准备的：全绿选延迟最低的 1~2 个即可，红了就换。

---

## 功能界面（面板一览）

- **总开关**：代理入口（端口 `8899`，可改）放行/拒绝。
- **① 自选上游**：类型（direct/http/socks5）+ 地址 + 可选认证 + ACL（默认仅放行 GitHub/Docker/GHCR/PyPI 等白名单域名，可切全局）+ 一键检测连通性。
- **② Docker Hub 加速**：编辑加速镜像列表 → 检测延迟 → 生成宿主 `daemon.json`（Supervised 用）或 **HAOS 持久化 udev 脚本**（复制到 SSH 终端即可）。
- **③ GHCR / HA 内核更新**：ghcr 镜像探活 + 生成「dockerd 走本插件代理」的 systemd / udev 配置。
- **④ HACS 官方组件加速**：开关 + 反代前缀（默认 `https://ghfast.top/`，可换成 `gh-proxy.com` 等）+ 状态（HACS 版本 / 补丁状态 / 备份存在 / 被覆盖提示）+ 一键重启 HA。
- **⑤ 链路诊断**：从插件容器探测 github.com / raw / api / ghcr.io / Docker Hub / PyPI 的可达性与延迟，帮判断该开哪一路。

---

## 安装

### 方式 A：添加为加载项仓库（推荐）
1. Home Assistant → **设置 → 加载项 → 加载项商店** → 右上角 ⋮ → **仓库**；
2. 填入本仓库 URL（推送到 GitHub 后：`https://github.com/<你的账号>/ha-proxy-accelerator`）；
3. 商店出现 **HA 代理加速器** → 安装 → 启动 → 打开侧边栏面板「HA 代理加速器」。

### 方式 B：本地加载项
把本仓库整个文件夹放到 HA 的 addons 共享目录（Samba 里的 `addons`，或 SSH 到 `/addons/ha-proxy-accelerator`），然后 **设置 → 加载项 → 本地加载项** 中刷新并安装。安装即在本机 Docker 构建（构建基础镜像来自 ghcr.io/home-assistant/*-base，需要能访问 ghcr 一次）。

> 面板通过 **Ingress** 提供，无需额外开放端口；代理入口 `8899/tcp` 会发布到宿主机供局域网使用。

---

## 快速开始

1. 打开面板，在 **①** 填你的代理（如 Clash：`192.168.1.2:7890`），点「保存 + 检测」——绿色即通；
2. 打开顶部**总开关**（局域网设备即可把 HTTP 代理指向 `HA主机IP:8899`）；
3. **②③**：想要 Docker Hub / ghcr 加速 → 点「检测」选绿源 → 点「生成配置」→ 按卡片里的说明在宿主机粘贴应用（Supervised 或 HAOS 各有一版）;
4. **④**：已装官方 HACS 的 → 打开「HACS 官方组件加速」开关 → 「应用加速补丁」→ 「应用后重启 HA」；
5. **⑤**：跑一次诊断确认 github / ghcr 现在是否可达。

---

## 宿主侧配置（②③ 的应用步骤）

### Docker Hub 加速
**Supervised（Debian 等）**：把面板生成的 `daemon.json` 内容写入 `/etc/docker/daemon.json`，然后：
```bash
sudo systemctl restart docker
```
⚠️ 之后 Supervisor 健康检查会标记 `unsupported: docker_configuration`（[官方说明](https://www.home-assistant.io/more-info/unsupported/docker_configuration/)）——功能可用，只是失去官方支持徽章，自行权衡。

**HAOS（系统盘只读）**：把面板生成的 udev 规则保存为
```
/etc/udev/rules.d/99-docker-mirror.rules
```
（通过官方 SSH & Web Terminal add-on 写入），然后**重启主机**。它会在每次开机把镜像源写进 `/etc/docker/daemon.json` 并刷新 dockerd，不随系统升级丢失。机制参考 [bbs.hassbian 26549](https://bbs.hassbian.com/forum.php?mod=viewthread&tid=26549)。

### GHCR 加速（HA 内核 / Supervisor 更新走代理）
让宿主 dockerd 走本插件的代理入口（先开总开关、确认 ① 上游可用）：

- **Supervised**：面板 ③ 生成的 `http-proxy.conf` 放到
  `/etc/systemd/system/docker.service.d/http-proxy.conf`，然后
  `sudo systemctl daemon-reload && sudo systemctl restart docker`。
- **HAOS**：使用面板 ③ 的「udev 全流量代理脚本」写 `/etc/udev/rules.d/99-docker-proxy.rules` 后重启主机。

此后 Supervisor 更新 core/add-on 时，dockerd 拉取 `ghcr.io/home-assistant/...` 会先连本插件、再走你的上游。若不用代理而只想用公共 ghcr 镜像前缀（`ghcr.nju.edu.cn` 等），Supervisor 无法改写镜像名（[讨论 #2797](https://github.com/home-assistant/operating-system/discussions/2797?sort=top)），只能靠上述代理或定制固件（如 [HAOS-CN](https://github.com/ha-china/HAOS-CN)）实现。

---

## HACS 加速说明

- 前置：**官方 HACS** 已安装（`/config/custom_components/hacs` 存在）。
- 开关打开后：`hacs_patch` 会把 HACS 代码中的 `https://github.com`、`api.github.com`、`raw.githubusercontent.com`、`codeload.github.com` 等地址改写为 `前缀 + 原地址`（形如 `https://ghfast.top/https://github.com/...`），备份存于 `custom_components/.hacs-accel-backup/`。
- **必须重启 Home Assistant** 才生效（面板提供一键重启）。
- 回滚：关掉开关即可从备份还原（或点「回滚」）。
- HACS 更新会覆盖补丁：面板状态会显示「补丁文件与备份不一致」，此时点「重新应用」即可。
- 思路与社区 [hacs-china / HACS 极速版](https://github.com/hacs-china/)、[Sheldondxx/integration-homeassist](https://github.com/Sheldondxx/integration-homeassist) 同源；本实现附加了自动备份/回滚/覆盖检测。

---

## 项目结构

```
ha-proxy-accelerator/
├── config.yaml            # add-on 清单(Ingress/端口/权限/默认选项/中文 schema)
├── build.json             # 多架构构建源(ghcr.io/home-assistant/*-base)
├── Dockerfile             # 极简镜像(仅 python3)
├── repository.json        # HA 加载项仓库注册信息
├── rootfs/
│   ├── run.sh             # 入口: 引导配置 → 启动 server
│   └── app/
│       ├── server.py      # asyncio 正向代理(8899) + 面板/API(8099)
│       ├── hacs_patch.py  # HACS 补丁(备份/回滚/覆盖检测)
│       ├── bootstrap.py   # options.json → settings.json 首启引导
│       └── web/           # 面板前端(index.html/style.css/app.js)
└── README.md
```

- 配置持久化：`/data/settings.json`；所有开关即时写入、连接级重读，无需重启。
- 纯 Python 3 标准库，无第三方依赖；镜像极小。
- 代理实现：CONNECT 隧道 + HTTP 绝对 URI 转发，上游支持 direct/http/socks5，域名 ACL。

本地自测（无需 HA）：
```bash
STATE_FILE=/tmp/settings.json HACS_DIR=/tmp/fakehacs python3 rootfs/app/server.py
python3 rootfs/app/hacs_patch.py status | apply 'https://ghfast.top/' | rollback
```

---

## FAQ

- **面板里 ① 全绿，HACS 下载还是慢？** HACS 加速走 ④（应用补丁 + 重启 HA），不是 ①。补丁被 HACS 更新覆盖后需重新应用。
- **为什么 ② 的 daemon.json 不能直接改？** add-on 沙箱无宿主文件系统权限（HAOS 只读）。这是安全边界；面板把配置生成到可复制的程度已经是最大能力。
- **为什么 ③ 说 registry-mirrors 没用？** 它只对 Docker Hub 生效，HA 内核镜像在 ghcr.io。要么 dockerd 走代理，要么用定制固件，没有第三条 add-on 内可自动化的路。
- **检测全部标红？** 当前网络到这些镜像/反代不通，或第三方服务已失效。可稍后再试/换清单里的其他源，或检查宿主防火墙。
- **镜像失效频繁怎么办？** 用面板编辑列表加入社区新源（参考 [kubesre/docker-registry-mirrors](https://github.com/kubesre/docker-registry-mirrors) 的最新清单），面板会记住你的列表。

---

## 致谢与借鉴

- [hacs-china / HACS 极速版](https://github.com/hacs-china/) 、[Sheldondxx/integration-homeassist](https://github.com/Sheldondxx/integration-homeassist) —— HACS 应用层加速思路
- [ha-china/HAOS-CN](https://github.com/ha-china/HAOS-CN)（大陆定制固件，DeepWiki [2.1 网络重定向](https://deepwiki.com/ha-china/HAOS-CN/2.1-network-service-redirection)/[2.2 Docker 镜像源](https://deepwiki.com/ha-china/HAOS-CN/2.2-docker-registry-mirrors)）、[ha-china/hassio-addons](https://github.com/ha-china/hassio-addons)
- [bbs.hassbian：udev 规则更换 hassos docker 镜像源](https://bbs.hassbian.com/forum.php?mod=viewthread&tid=26549)
- [home-assistant/operating-system Discussion #2797：How can I change image registry?](https://github.com/home-assistant/operating-system/discussions/2797?sort=top)
- [kubesre/docker-registry-mirrors](https://github.com/kubesre/docker-registry-mirrors)、[SeanChang/xuanyuan_docker_proxy](https://github.com/SeanChang/xuanyuan_docker_proxy) —— 镜像站清单与架构
- Home Assistant 官方文档：[Add-on 开发（Apps）](https://developers.home-assistant.io/docs/apps/)、[Add-on 配置字段](https://developers.home-assistant.io/docs/add-ons/configuration)、[unsupported: docker_configuration](https://www.home-assistant.io/more-info/unsupported/docker_configuration/)
- 结构参考：hassio-addons/adguard-home（Ingress + 大 schema 样板）、kapuic/hassio-mihomo 与 ha-china/hassio-addons（代理类 add-on 样例）

## 免责声明

- 第三方镜像站 / GitHub 反代随时可能失效或变更服务条款，请自行甄别，本插件不对其可用性负责。
- 修改宿主 Docker 配置可能触发官方 `unsupported` 标记；操作前请备份原配置。
- HACS 补丁会改动第三方集成文件，本插件提供完整备份与回滚，但请知悉风险后再启用。
