/* HA 代理加速器 — 面板逻辑 (相对路径, 兼容 Ingress 子路径) */
"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let toastTimer = null;

function toast(msg, kind) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show" + (kind === "err" ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 3200);
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error("HTTP " + r.status + " " + path);
  return r.json();
}

async function refreshState() {
  state = await api("GET", "api/state");
  renderState();
}

function renderState() {
  $("mode").value = state.mode || "http";
  $("upstream").value = state.upstream || "";
  $("upUser").value = state.upstream_user || "";
  $("upPass").value = "";
  $("aclMode").value = state.acl_mode || "whitelist";
  $("whitelist").value = (state.whitelist || []).join("\n");
  $("hubMirrors").value = (state.docker_hub_mirrors || []).join("\n");
  $("ghcrMirrors").value = (state.ghcr_mirrors || []).join("\n");
  $("hacsPrefix").value = state.hacs_github_proxy || "";
  $("swProxy").checked = !!state.enabled;
  renderMaster();
}

function renderMaster() {
  const on = !!state.enabled;
  $("proxyStateDot").className = "dot " + (on ? "on" : "off");
  $("proxyStateText").textContent = "代理入口: " + (on ? "开启" : "关闭");
  $("proxyStateText").style.color = on ? "var(--green)" : "var(--red)";
  $("swProxy").checked = on;
  $("proxyPortLbl").textContent = window.PROXY_PORT_HINT || "8899";
}

/* ---------------- 上游 ---------------- */

async function saveUpstream() {
  const whitelist = $("whitelist").value
    .split("\n").map((s) => s.trim().toLowerCase()).filter(Boolean);
  const patch = {
    mode: $("mode").value,
    upstream: $("upstream").value.trim(),
    upstream_user: $("upUser").value.trim(),
    upstream_pass: $("upPass").value,
    acl_mode: $("aclMode").value,
    whitelist,
  };
  try {
    state = await api("POST", "api/state", patch);
    showResult("upstreamResult", "已保存 ✓", "");
    renderState();
  } catch (e) { showResult("upstreamResult", "保存失败: " + e.message, "err"); }
}

async function probeUpstream() {
  const mode = $("mode").value;
  if (mode === "direct") {
    showResult("upstreamResult", "direct 模式无需上游代理。", "info");
    return;
  }
  const addr = $("upstream").value.trim() || "127.0.0.1:7890";
  const btn = $("btnProbeUpstream");
  btn.disabled = true; btn.textContent = "检测中…";
  showResult("upstreamResult", "", "");
  try {
    const r = await api("POST", "api/probe", {
      targets: [{ id: "up", mode, addr }],
    });
    const t = r["up"];
    if (t && t.ok) showResult("upstreamResult", "上游可达 ✓ 延迟 " + t.ms + " ms", "");
    else showResult("upstreamResult", "上游不可达: " + ((t && (t.error || t.info)) || "?"), "err");
  } catch (e) {
    showResult("upstreamResult", "检测失败: " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "检测上游连通性";
  }
}

function showResult(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = "result" + (kind ? " " + kind : "");
}

/* ---------------- 镜像检测 ---------------- */

async function checkMirrors(force) {
  const btnHub = $("btnCheckHub"), btnGh = $("btnCheckGhcr");
  [btnHub, btnGh].forEach((b) => (b.disabled = true));
  try {
    const m = await api("GET", "api/mirrors");
    renderMirrorList("hubResult", m.docker_hub);
    renderMirrorList("ghcrResult", m.ghcr);
  } catch (e) {
    toast("镜像检测失败: " + e.message, "err");
  } finally {
    [btnHub, btnGh].forEach((b) => (b.disabled = false));
  }
}

function renderMirrorList(id, list) {
  const ul = $(id);
  ul.innerHTML = "";
  for (const m of list || []) {
    const li = document.createElement("li");
    const cls = m.ok ? "ok" : "bad";
    const t = m.ok
      ? "✓ " + m.ms + " ms"
      : "✗ 不可达" + (m.info && m.info.startsWith("HTTP") ? " (" + m.info + ")" : "");
    li.innerHTML = `<span class="${cls}">${t}</span><code>${esc(m.url)}</code>`;
    ul.appendChild(li);
  }
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------- daemon.json / 宿主配置生成 ---------------- */

function hubMirrorList() {
  return $("hubMirrors").value.split("\n").map((s) => s.trim()).filter(Boolean);
}
function ghcrMirrorList() {
  return $("ghcrMirrors").value.split("\n").map((s) => s.trim()).filter(Boolean);
}

function genDaemonJson() {
  const list = hubMirrorList();
  const mirrors = list.filter((u) => u.startsWith("https://"));
  const insecure = list.filter((u) => u.startsWith("http://"))
    .map((u) => u.replace(/^https?:\/\//, "").replace(/\/+$/, ""));
  const cfg = { "registry-mirrors": mirrors };
  if (insecure.length) cfg["insecure-registries"] = insecure;
  $("daemonJson").textContent = JSON.stringify(cfg, null, 2) + "\n# 适用: Supervised/可写 daemon.json 的主机";
  return cfg;
}

function genUdevHub() {
  const list = hubMirrorList().filter((u) => u.startsWith("https://"));
  if (!list.length) { toast("请先填写 Hub 加速镜像", "err"); return; }
  const mirrors = list.map((u) => `"${u}"`).join(", ");
  return `# HAOS 开机持久化 Docker registry-mirrors (写入 /etc/udev/rules.d/99-docker-mirror.rules)
# 来源: HA 代理加速器 ② Docker Hub 镜像加速
ACTION=="add", SUBSYSTEM=="net", KERNEL=="eth0", RUN+="/bin/sh -c 'mkdir -p /etc/docker && printf \\"{\\\\\\"registry-mirrors\\\\\\": [${mirrors.replace(/\\\\/g, "\\\\\\\\")}]}\\n\\" > /etc/docker/daemon.json && pkill -HUP dockerd || true'"`;
}

function copyText(text, label) {
  const doCopy = async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta);
        ta.select(); document.execCommand("copy"); ta.remove();
      }
      toast((label || "内容") + " 已复制");
    } catch (e) { toast("复制失败: " + e.message, "err"); }
  };
  doCopy();
}

/* ---------------- HACS ---------------- */

async function loadHacsStatus() {
  const p = $("hacsStatus");
  try {
    const s = await api("GET", "api/hacs/status");
    $("swHacs").disabled = false;
    renderHacs(s);
  } catch (e) {
    p.innerHTML = '<span class="r">HACS 状态不可用: ' + esc(e.message) +
      "</span><br><span class='muted'>若在非 add-on 环境或补丁模块缺失, 本功能不可用。</span>";
    $("swHacs").disabled = true;
  }
}

function renderHacs(s) {
  const p = $("hacsStatus");
  p.innerHTML = "";
  const row = (k, v, cls) => {
    const d = document.createElement("div");
    d.innerHTML = `<b>${k}:</b> <span class="${cls || ""}">${esc(v)}</span>`;
    p.appendChild(d);
  };
  if (!s.hacs_installed) row("HACS", "未在 custom_components/hacs 检测到 (官方 HACS 未安装?)", "r");
  else {
    row("HACS 版本", s.version || "?", s.patched ? "g" : "");
    row("补丁状态", s.patched ? "已加速 (启用中)" : "未打补丁 (官方原版)", s.patched ? "g" : "");
    row("备份", s.backup_exists ? "已存在(可回滚)" : "无");
    if (s.note) { const n = document.createElement("div"); n.className = "tip"; n.textContent = s.note; p.appendChild(n); }
    $("swHacs").checked = !!s.patched;
  }
}

async function hacsApply() {
  const enabled = $("swHacs").checked;
  const patch = { enabled, hacs_github_proxy: $("hacsPrefix").value.trim() || "https://ghfast.top/" };
  // 先保存前缀到状态, 再应用
  try {
    await api("POST", "api/state", { hacs_github_proxy: patch.hacs_github_proxy });
  } catch (e) { /* ignore */ }
  const btn = enabled ? $("btnHacsApply") : $("btnHacsRollback");
  btn.disabled = true; btn.textContent = "执行中…";
  try {
    const r = await api("POST", "api/hacs/apply", { enabled, hacs_github_proxy: patch.hacs_github_proxy });
    if (r.ok) {
      toast(enabled ? "补丁已应用 ✓ (建议重启 HA)" : "已回滚 ✓");
      await loadHacsStatus();
    } else {
      toast("操作失败: " + (r.error || "?"), "err");
      await loadHacsStatus();
    }
  } catch (e) {
    toast("请求失败: " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = enabled ? "应用加速补丁" : "回滚(关闭补丁)";
  }
}

async function restartHa() {
  if (!confirm("确认重启 Home Assistant? 重启约需 1~2 分钟。")) return;
  try {
    const r = await api("POST", "api/restart-ha", {});
    if (r.ok) toast("已请求重启 HA ✓");
    else toast("无法自动重启: " + (r.error || "?"), "err");
  } catch (e) { toast("请求失败: " + e.message, "err"); }
}

/* ---------------- 诊断 ---------------- */

const DIAG_TARGETS = [
  { id: "github", url: "https://github.com/", label: "github.com (HACS 网页)" },
  { id: "raw", url: "https://raw.githubusercontent.com/", label: "raw.githubusercontent.com" },
  { id: "api", url: "https://api.github.com/", label: "api.github.com (HACS API)" },
  { id: "ghcr", url: "https://ghcr.io/v2/", label: "ghcr.io (HA 内核镜像)" },
  { id: "hub", url: "https://registry-1.docker.io/v2/", label: "registry-1.docker.io (Docker Hub)" },
  { id: "pypi", url: "https://pypi.org/simple/", label: "pypi.org (Python 依赖)" },
];

async function runDiag() {
  const tbody = $("diagBody");
  tbody.innerHTML = "";
  for (const t of DIAG_TARGETS) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(t.label)}</td><td class="r">检测中…</td><td>-</td><td></td>`;
    tbody.appendChild(tr);
  }
  const btn = $("btnDiag"); btn.disabled = true; btn.textContent = "诊断中…";
  try {
    const r = await api("POST", "api/probe", {
      targets: DIAG_TARGETS.map((t) => ({ id: t.id, kind: "https", url: t.url })),
    });
    DIAG_TARGETS.forEach((t, i) => {
      const info = r[t.id] || {};
      const tr = tbody.children[i];
      const ok = info.ok;
      tr.innerHTML = `<td>${esc(t.label)}</td>` +
        `<td class="${ok ? "g" : "r"}">${ok ? "✓ 可达" : "✗ 不可达"}</td>` +
        `<td>${info.ok ? info.ms + " ms" : "-"}</td>` +
        `<td class="muted">${esc((info.info || info.error || "").slice(0, 60))}</td>`;
    });
  } catch (e) {
    toast("诊断失败: " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "开始诊断";
  }
}

/* ---------------- 事件绑定 ---------------- */

function bind() {
  $("swProxy").addEventListener("change", async (e) => {
    try {
      state = await api("POST", "api/toggle", { enabled: e.target.checked });
      renderMaster();
      toast(e.target.checked ? "代理入口已开启" : "代理入口已关闭");
    } catch (err) {
      toast("切换失败: " + err.message, "err");
      renderState();
    }
  });

  $("btnSaveUpstream").addEventListener("click", saveUpstream);
  $("btnProbeUpstream").addEventListener("click", probeUpstream);
  $("btnResetWhitelist").addEventListener("click", async () => {
    try {
      const fresh = await api("GET", "api/state");
      $("whitelist").value = fresh.whitelist.join("\n");
      toast("已恢复默认白名单(未保存, 点击「保存上游配置」生效)");
    } catch (e) { toast(e.message, "err"); }
  });

  $("btnCheckHub").addEventListener("click", () => saveMirrorsThen(() => checkMirrors(true)));
  $("btnCheckGhcr").addEventListener("click", () => saveMirrorsThen(() => checkMirrors(true)));

  $("btnGenDaemon").addEventListener("click", () => genDaemonJson());
  $("btnCopyDaemon").addEventListener("click", () => {
    const cfg = genDaemonJson();
    copyText(JSON.stringify(cfg, null, 2), "daemon.json");
  });
  $("btnCopyUdev").addEventListener("click", () => {
    genDaemonJson();
    copyText(genUdevHub(), "HAOS udev 脚本");
  });

  $("btnCopyDockerProxy").addEventListener("click", () => {
    copyText(genDockerProxySystemd(), "dockerd 代理配置");
  });
  $("btnCopyUdevProxy").addEventListener("click", () => {
    copyText(genDockerProxyUdev(), "HAOS 全流量代理 udev 脚本");
  });

  $("swHacs").addEventListener("change", hacsApply);
  $("btnHacsApply").addEventListener("click", () => { $("swHacs").checked = true; hacsApply(); });
  $("btnHacsRollback").addEventListener("click", () => { $("swHacs").checked = false; hacsApply(); });
  $("btnRestartHa").addEventListener("click", restartHa);

  $("btnDiag").addEventListener("click", runDiag);
}

async function saveMirrorsThen(fn) {
  try {
    const patch = {
      docker_hub_mirrors: hubMirrorList(),
      ghcr_mirrors: ghcrMirrorList(),
    };
    state = await api("POST", "api/state", patch);
    await fn();
  } catch (e) { toast("保存镜像列表失败: " + e.message, "err"); }
}

function proxyEntryHint() {
  // 尽力推断宿主 IP(经 Ingress 请求头)
  return "";
}

function genDockerProxySystemd() {
  const addr = $("upstream").value.trim() || "127.0.0.1:7890";
  const haHost = window.location.hostname || "<HA主机IP>";
  const entry = `http://${haHost}:8899`;
  return `# dockerd 走本插件代理 (写入 /etc/systemd/system/docker.service.d/http-proxy.conf)
# 前提: 面板「代理入口总开关」已开, 上游可用; 本插件发布端口 8899
[Service]
Environment="HTTP_PROXY=${entry}"
Environment="HTTPS_PROXY=${entry}"
Environment="NO_PROXY=localhost,127.0.0.1,172.30.0.0/16,172.30.32.3,.local,.home.arpa"

# 应用:
#   sudo mkdir -p /etc/systemd/system/docker.service.d
#   sudo cp http-proxy.conf /etc/systemd/system/docker.service.d/
#   sudo systemctl daemon-reload && sudo systemctl restart docker
# 验证: sudo systemctl show docker --property=Environment`;
}

function genDockerProxyUdev() {
  const haHost = window.location.hostname || "<HA主机IP>";
  const entry = `http://${haHost}:8899`;
  return `# HAOS: 开机把 dockerd 代理注入 docker 服务(写入 /etc/udev/rules.d/99-docker-proxy.rules)
# 前提: 面板「代理入口总开关」已开; 8899 为插件发布端口
ACTION=="add", SUBSYSTEM=="net", KERNEL=="eth0", RUN+="/bin/sh -c 'mkdir -p /etc/systemd/system/docker.service.d && printf \\"[Service]\\\\nEnvironment=\\\\\\"HTTP_PROXY=${entry}\\\\\\"\\\\nEnvironment=\\\\\\"HTTPS_PROXY=${entry}\\\\\\"\\\\nEnvironment=\\\\\\"NO_PROXY=localhost,127.0.0.1,172.30.0.0/16,172.30.32.3,.local,.home.arpa\\\\\\"\\\\n\\" > /etc/systemd/system/docker.service.d/http-proxy.conf && pkill -HUP dockerd || true'"`;
}

document.addEventListener("DOMContentLoaded", async () => {
  bind();
  try {
    await refreshState();
  } catch (e) {
    toast("无法读取配置: " + e.message, "err");
  }
  checkMirrors(false).catch(() => {});
  loadHacsStatus().catch(() => {});
});
