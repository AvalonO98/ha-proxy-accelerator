/* Net Proxy 面板前端：无框架、无构建步骤。
   所有请求都走相对路径，因此在 Supervisor Ingress 的
   /api/hassio_ingress/<token>/ 前缀下天然可用。 */

const I18N = {
  zh: {
    title: "网络代理", subtitle: "让 HA 系统更新与插件安装的镜像拉取走代理 / 加速源",
    state_off: "未启用", state_on: "已生效", state_pending: "待重启校验",
    state_err: "已启用但未生效", state_busy: "执行中…", state_noverify: "已启用（无校验）",
    sec_mode: "1 · 选择代理方式", sec_mode_hint: "同一时间只启用一种方式",
    sec_mirrors: "2 · 镜像源（Docker / GitHub 均可自定义）",
    sec_mirrors_hint: "两类都可以增删自定义条目，不限于内置的几个",
    gh_mirrors: "GitHub 加速源（每行一个，按顺序自动回退）",
    gh_hint: "前缀式：https://ghfast.top/ → https://ghfast.top/https://github.com/... ；也支持含 {url} 的模板。",
    kernel_ver: "mihomo 内核版本（可空 = 最新）",
    ghcr_note: "注意：dockerd 的 registry-mirrors 只对 Docker Hub 生效，ghcr.io 的镜像拉取请用方式 B（HTTP/SOCKS5 代理）。",
    mode_a_hint: "加速镜像地址在下面的「镜像源」区域填写（可自由增删自定义条目）。改动只需要 SIGHUP 热加载，不会中断 HA。",
    mode_a: "Docker 加速镜像", mode_a_l1: "改写 registry-mirrors，dockerd 热加载",
    mode_a_l2: "不重启、不中断 HA", mode_a_l3: "只对 Docker Hub 生效（ghcr.io 不受它影响）",
    mode_b: "上游代理 / 订阅", mode_b_l1: "内置 mihomo，本地开 127.0.0.1:7890",
    mode_b_l2: "dockerd 走代理，覆盖 ghcr.io 与 Docker Hub", mode_b_l3: "需要重启一次 dockerd（HA 短暂中断）",
    mode_c: "内置 WireGuard 出口", mode_c_l1: "粘贴 .conf 即可，userspace 实现",
    mode_c_l2: "不改主机路由、不建 tun，风险小", mode_c_l3: "需要重启一次 dockerd（HA 短暂中断）",
    mirrors: "加速镜像地址（每行一个）",
    mirrors_hint: "地址需带 http(s)://；改动只需要 SIGHUP 热加载，不会中断 HA。",
    up_type: "上游类型", up_none: "不使用固定上游", up_url: "上游地址", up_user: "用户名（可空）", up_pass: "密码（可空）",
    sub_url: "订阅地址（Clash/mihomo）", sub_name: "订阅名称",
    sub_hint: "订阅由内核自己拉取；如果订阅地址也被墙，可把订阅内容保存成 /share/netproxy/subscription.yaml，内核会自动改用本地文件。",
    wg_conf: "WireGuard 配置（标准 .conf）", wg_check: "解析并校验",
    advanced: "高级设置", dns: "DNS（每行一个）", kernel_url: "内核下载地址（可空）",
    auto_rollback: "代理不健康时自动回滚并关闭", health_s: "容忍时长(秒)", apply_t: "应用超时(秒)",
    btn_apply: "应用并生效", btn_save: "仅保存", btn_install_kernel: "安装/更新内核",
    btn_kernel_restart: "重启内核", btn_rollback: "回滚到应用前",
    btn_test: "检测连通性（直连 vs 代理）", btn_pull: "实测拉取镜像",
    btn_pause: "暂停", btn_clear_view: "清屏",
    btn_self_heal: "一键修复权限（关闭保护模式并重启插件）",
    confirm_self_heal: "将调用 Supervisor 关闭本插件的「保护模式」并重启插件。这是 host_pid / docker_api 生效的前提，插件本身无法声明该设置。确认继续？",
    self_heal_started: "已请求关闭保护模式，插件正在重启…约 10 秒后本页面会自动重新加载。",
    sec_verify: "3 · 真实生效状态", sec_verify_hint: "数据来自 dockerd 实时配置与宿主机文件，不是只看插件自己的设置",
    sec_log: "4 · 运行日志", sec_hist: "5 · 变更历史与备份", hist: "变更历史", backups: "daemon.json 备份",
    raw: "当前 daemon.json（宿主）",
    footer: "提示：方式 B/C 会重启 dockerd，所有容器（含 HA 核心）会一起重启，通常 30–60 秒恢复。所有写入都会先备份，校验失败会自动回滚。",
    k_cap: "宿主访问能力", k_docker: "dockerd", k_mirrors: "Registry Mirrors（实时）",
    k_proxy: "HTTP(S) Proxy（实时）", k_daemon: "daemon.json", k_kernel: "mihomo 内核",
    k_verify: "校验结论", k_apply: "上一次应用结果", k_wg: "WireGuard", k_noproxy: "NoProxy",
    none: "（空）", yes: "是", no: "否", okv: "通过", badv: "未通过", unknown: "—",
    confirm_rollback: "确定要回滚 daemon.json 并关闭总开关吗？",
    confirm_restart: "方式 B/C 需要重启 dockerd：所有容器（含 HA 核心）会一起重启，HA 会短暂中断。确认继续？",
    saved: "已保存。点「应用并生效」才会真正改动系统。",
    direct: "直连", via_proxy: "走代理", mirrors_reach: "Docker 加速镜像可达性", github_reach: "GitHub 直连 / 加速源可达性",
  },
  en: {
    title: "Net Proxy", subtitle: "Route HA updates and add-on image pulls through a proxy / mirror",
    state_off: "Disabled", state_on: "Active", state_pending: "Awaiting restart check",
    state_err: "Enabled but not effective", state_busy: "Running…", state_noverify: "Enabled (no check)",
    sec_mode: "1 · Choose proxy mode", sec_mode_hint: "Only one mode is active at a time",
    sec_mirrors: "2 · Mirrors (Docker / GitHub, both customisable)",
    sec_mirrors_hint: "Both lists accept arbitrary custom entries",
    gh_mirrors: "GitHub acceleration prefixes (one per line, tried in order)",
    gh_hint: "Prefix form: https://ghfast.top/ → https://ghfast.top/https://github.com/... ; a {url} template also works.",
    kernel_ver: "mihomo kernel version (empty = latest)",
    ghcr_note: "Note: dockerd's registry-mirrors only covers Docker Hub; use mode B (HTTP/SOCKS5 proxy) for ghcr.io pulls.",
    mode_a_hint: "Configure registry mirrors in the “Mirrors” section below (any custom entries). Applied with SIGHUP hot reload — no downtime.",
    mode_a: "Docker registry mirror", mode_a_l1: "Rewrites registry-mirrors; dockerd hot-reloads",
    mode_a_l2: "No restart, no HA downtime", mode_a_l3: "Docker Hub only (does not affect ghcr.io)",
    mode_b: "Upstream proxy / subscription", mode_b_l1: "Bundled mihomo on 127.0.0.1:7890",
    mode_b_l2: "dockerd uses the proxy for ghcr.io and Docker Hub", mode_b_l3: "Needs one dockerd restart (brief HA downtime)",
    mode_c: "Built-in WireGuard egress", mode_c_l1: "Paste a .conf; userspace implementation",
    mode_c_l2: "No host routes, no tun device", mode_c_l3: "Needs one dockerd restart (brief HA downtime)",
    mirrors: "Registry mirrors (one per line)",
    mirrors_hint: "Must include http(s)://. Applied with SIGHUP hot reload — no downtime.",
    up_type: "Upstream type", up_none: "No fixed upstream", up_url: "Upstream URL",
    up_user: "Username (optional)", up_pass: "Password (optional)",
    sub_url: "Subscription URL (Clash/mihomo)", sub_name: "Subscription name",
    sub_hint: "Fetched by the core. If the URL is blocked too, save the subscription as /share/netproxy/subscription.yaml and the core will use the local file.",
    wg_conf: "WireGuard config (standard .conf)", wg_check: "Parse & validate",
    advanced: "Advanced", dns: "DNS (one per line)", kernel_url: "Kernel download URL (optional)",
    auto_rollback: "Auto rollback and disable when unhealthy", health_s: "Grace period (s)", apply_t: "Apply timeout (s)",
    btn_apply: "Apply & activate", btn_save: "Save only", btn_install_kernel: "Install/update kernel",
    btn_kernel_restart: "Restart kernel", btn_rollback: "Roll back to pre-apply",
    btn_test: "Test connectivity (direct vs proxy)", btn_pull: "Test image pull",
    btn_pause: "Pause", btn_clear_view: "Clear",
    btn_self_heal: "Fix permissions automatically (disable protection mode & restart)",
    confirm_self_heal: "This asks Supervisor to disable protection mode for this add-on and restart it. Protection mode is a user-level setting the add-on cannot declare, and host_pid / docker_api only work when it is off. Continue?",
    self_heal_started: "Protection mode disabling requested; the add-on is restarting… this page reloads in ~10s.",
    sec_verify: "3 · Actual live state", sec_verify_hint: "Read from dockerd live config and host files, not from this add-on's own settings",
    sec_log: "4 · Logs", sec_hist: "5 · History & backups", hist: "History", backups: "daemon.json backups",
    raw: "Current daemon.json (host)",
    footer: "Modes B/C restart dockerd: all containers (including HA core) restart together, typically back in 30–60s. Every write is backed up and auto-rolls back on verification failure.",
    k_cap: "Host access", k_docker: "dockerd", k_mirrors: "Registry Mirrors (live)",
    k_proxy: "HTTP(S) Proxy (live)", k_daemon: "daemon.json", k_kernel: "mihomo kernel",
    k_verify: "Verification", k_apply: "Last apply result", k_wg: "WireGuard", k_noproxy: "NoProxy",
    none: "(empty)", yes: "yes", no: "no", okv: "pass", badv: "fail", unknown: "—",
    confirm_rollback: "Roll back daemon.json and disable the proxy?",
    confirm_restart: "Modes B/C restart dockerd: every container (including HA core) restarts and HA is briefly unavailable. Continue?",
    saved: "Saved. Press “Apply & activate” to actually change the system.",
    direct: "Direct", via_proxy: "Via proxy", mirrors_reach: "Docker mirror reachability", github_reach: "GitHub direct / accelerator reachability",
  },
};

let LANG = (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";
let S = null, CFG = null, dirty = false, paused = false, busy = false, logSeq = 0;

const T = (k) => (I18N[LANG][k] !== undefined ? I18N[LANG][k] : k);
const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opt = { headers: {} };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { data = { error: "响应不是 JSON" }; }
  if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
  return data;
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((e) => { e.textContent = T(e.dataset.i18n); });
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  $("langBtn").textContent = LANG === "zh" ? "EN" : "中文";
}

function lines(v) { return (v || "").split("\n").map((s) => s.trim()).filter(Boolean); }

function readForm() {
  const c = CFG || {};
  c.enabled = $("master").checked;
  c.mode = document.querySelector(".mode.active")?.dataset.mode || c.mode || "docker_mirror";
  c.mirrors = lines($("mirrors").value);
  c.github_mirrors = lines($("gh-mirrors").value);
  c.kernel_version = $("kernel-ver").value.trim();
  c.upstream = { type: $("up-type").value, url: $("up-url").value.trim(), username: $("up-user").value.trim(), password: $("up-pass").value };
  c.subscription = { url: $("sub-url").value.trim(), profile: $("sub-name").value.trim() };
  c.wireguard = { config: $("wg-config").value };
  c.dns = lines($("dns").value);
  c.kernel_url = $("kernel-url").value.trim();
  c.auto_rollback = $("auto-rollback").checked;
  c.health_check_seconds = parseInt($("health-s").value || "300", 10);
  c.apply_timeout_seconds = parseInt($("apply-t").value || "180", 10);
  return c;
}

function writeForm(cfg) {
  $("master").checked = !!cfg.enabled;
  $("mirrors").value = (cfg.mirrors || []).join("\n");
  $("gh-mirrors").value = (cfg.github_mirrors || []).join("\n");
  $("kernel-ver").value = cfg.kernel_version || "";
  $("up-type").value = (cfg.upstream && cfg.upstream.type) || "none";
  $("up-url").value = (cfg.upstream && cfg.upstream.url) || "";
  $("up-user").value = (cfg.upstream && cfg.upstream.username) || "";
  $("up-pass").value = (cfg.upstream && cfg.upstream.password) || "";
  $("sub-url").value = (cfg.subscription && cfg.subscription.url) || "";
  $("sub-name").value = (cfg.subscription && cfg.subscription.profile) || "";
  $("wg-config").value = (cfg.wireguard && cfg.wireguard.config) || "";
  $("dns").value = (cfg.dns || []).join("\n");
  $("kernel-url").value = cfg.kernel_url || "";
  $("auto-rollback").checked = !!cfg.auto_rollback;
  $("health-s").value = cfg.health_check_seconds || 300;
  $("apply-t").value = cfg.apply_timeout_seconds || 180;
  setMode(cfg.mode || "docker_mirror", false);
}

function setMode(m, mark) {
  document.querySelectorAll(".mode").forEach((e) => e.classList.toggle("active", e.dataset.mode === m));
  document.querySelectorAll(".mode-panel").forEach((e) => e.classList.remove("active"));
  const p = $("panel-" + m);
  if (p) p.classList.add("active");
  if (mark) dirty = true;
}

function kv(k, v, cls) {
  return `<div class="kv"><div class="k">${k}</div><div class="v ${cls || ""}">${v === undefined || v === null || v === "" ? T("none") : v}</div></div>`;
}

function esc(s) {
  return String(s === undefined || s === null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function render() {
  if (!S) return;
  const cap = S.capability || {}, d = S.docker || {}, k = S.kernel || {}, ap = S.applied || {};

  // 状态徽标
  const pill = $("statePill");
  let cls = "pill pill-off", txt = T("state_off");
  if (busy) { cls = "pill pill-wait"; txt = T("state_busy"); }
  else if (ap.pending_verify) { cls = "pill pill-wait"; txt = T("state_pending"); }
  else if (S.enabled && S.verify && S.verify.ok) { cls = "pill pill-on"; txt = T("state_on"); }
  else if (S.enabled && S.verify && !S.verify.ok) { cls = "pill pill-err"; txt = T("state_err"); }
  else if (S.enabled) { cls = "pill pill-wait"; txt = T("state_noverify"); }
  pill.className = cls; pill.textContent = txt;

  // 横幅
  const bs = [];
  if (!cap.host_access) {
    const why = cap.reason ? esc(cap.reason) : "";
    bs.push(`<div class="banner err">${LANG === "zh"
      ? "拿不到宿主机文件系统访问权限（当前模式 " + esc(cap.namespace_mode) + "），方式 A/B/C 都无法生效。<br>"
        + "最常见原因：插件处于 Supervisor 的<b>保护模式</b>——保护模式下 host_pid / docker_api 不会生效。"
      : "No host filesystem access (mode " + esc(cap.namespace_mode) + "); modes A/B/C cannot take effect.<br>"
        + "Most likely cause: the add-on runs in Supervisor <b>protection mode</b>, which disables host_pid / docker_api."}
      <div style="margin-top:8px"><button class="primary" onclick="selfHeal()">${T("btn_self_heal")}</button></div>
      ${why}</div>`);
  }
  if (ap.pending_verify) {
    bs.push(`<div class="banner warn">${LANG === "zh"
      ? "正在等待 dockerd 重启完成后的自动校验；失败会自动回滚并关闭总开关。"
      : "Waiting for the post-restart verification; on failure it auto-rolls back and disables."}</div>`);
  }
  if (S.apply_result && S.apply_result.ok === 0) {
    bs.push(`<div class="banner err">${LANG === "zh"
      ? "上一次重启 dockerd 后健康检查失败，已自动回滚原配置。"
      : "Last dockerd restart failed its health check; the previous config was restored."}</div>`);
  }
  if (S.enabled && (S.mode === "upstream_proxy" || S.mode === "wireguard") && !k.running) {
    const hint = k.installed
      ? (LANG === "zh" ? "请点「重启内核」查看原因。" : " press “Restart kernel” to see why.")
      : (LANG === "zh" ? "请先点「安装/更新内核」。" : " press “Install/update kernel” first.");
    const head = LANG === "zh" ? "当前方式需要 mihomo 内核，但它没有运行，" : "This mode needs the mihomo kernel but it is not running;";
    bs.push(`<div class="banner warn">${head}${hint}</div>`);
  }
  $("banners").innerHTML = bs.join("");

  // 校验网格
  const g = [];
  g.push(kv(T("k_cap"), (cap.host_access ? "host:" : "local:") + esc(cap.namespace_mode) + " · nsenter " + (cap.nsenter ? "✓" : "✗"), cap.host_access ? "v-ok" : "v-bad"));
  g.push(kv(T("k_docker"), d.available ? ("v" + esc(d.server_version) + " · " + esc(d.storage_driver)) : esc(d.error), d.available ? "" : "v-bad"));
  g.push(kv(T("k_mirrors"), esc((d.registry_mirrors || []).join(" ")) || T("none"),
    (S.verify && S.verify.ok === false && (S.verify.problems || []).some((p) => p.indexOf("registry") >= 0)) ? "v-bad" : ""));
  g.push(kv(T("k_proxy"), "http: " + (esc(d.http_proxy) || T("none")) + "<br>https: " + (esc(d.https_proxy) || T("none")),
    d.http_proxy ? "v-ok" : ""));
  g.push(kv(T("k_daemon"), (S.daemon_json && S.daemon_json.existed ? "存在" : "不存在") + " · " + esc(Object.keys((S.daemon_json || {}).data || {}).join(", ")),
    (S.daemon_json && S.daemon_json.ok) ? "" : "v-bad"));
  g.push(kv(T("k_kernel"), (k.installed ? "✓ " + esc(k.version) : "✗ 未安装") + (k.running ? " · 运行中 pid=" + esc(k.pid) : " · 未运行") + (k.listen_ok ? " · 端口监听" : ""),
    k.running && k.listen_ok ? "v-ok" : (S.mode === "docker_mirror" ? "" : "v-warn")));
  if (S.mode === "wireguard" && S.wireguard) {
    g.push(kv(T("k_wg"), S.wireguard.valid ? (esc(S.wireguard.endpoint) + " · ip " + esc(S.wireguard.ip) + " · " + S.wireguard.peer_count + " peer") : esc(S.wireguard.error),
      S.wireguard.valid ? "v-ok" : "v-bad"));
  }
  g.push(kv(T("k_verify"), S.enabled
    ? (S.verify ? (S.verify.ok ? T("okv") : T("badv") + "<br>" + esc((S.verify.problems || []).join("<br>"))) : T("unknown"))
    : "—", S.enabled ? (S.verify && S.verify.ok ? "v-ok" : "v-bad") : ""));
  const ar = S.apply_result;
  g.push(kv(T("k_apply"), ar ? ("ok=" + esc(ar.ok) + (ar.finished ? " · " + new Date(ar.finished * 1000).toLocaleString() : "")) : T("unknown"),
    ar ? (ar.ok ? "v-ok" : "v-bad") : ""));
  $("verifyGrid").innerHTML = g.join("");

  // NoProxy 提示
  $("applyHint").innerHTML = `<div class="hint">NoProxy: ${esc((S.no_proxy || "").slice(0, 260))}</div>`;

  // 历史与备份
  $("hist").innerHTML = (S.history || []).slice(0, 20).map((h) =>
    `<div class="row"><span>${esc(h.kind)} · ${esc(h.detail)}</span><span class="t">${new Date(h.ts * 1000).toLocaleString()}</span></div>`).join("")
    || `<div class="row"><span>${T("none")}</span></div>`;
  $("backups").innerHTML = (S.backups || []).slice(0, 12).map((b) =>
    `<div class="row"><span>${esc(b.id)}<br><span class="t">existed=${esc(b.existed)}</span></span>
     <button class="chip" onclick="rollbackTo('${esc(b.id)}')">${LANG === "zh" ? "回滚" : "restore"}</button></div>`).join("")
    || `<div class="row"><span>${T("none")}</span></div>`;

  // 原始 daemon.json
  const raw = (S.daemon_json && S.daemon_json.data) ? JSON.stringify(S.daemon_json.data, null, 2) : (S.daemon_json && S.daemon_json.error) || "(不存在)";
  $("rawJson").textContent = raw;

  if (!dirty) { CFG = S.config; writeForm(S.config); }
}

async function refresh() {
  try {
    S = await api("api/status");
    render();
  } catch (e) { /* 忽略瞬时错误 */ }
}

async function pollLogs() {
  if (paused) return;
  try {
    const r = await api("api/logs?since=" + logSeq);
    if (r.logs && r.logs.length) {
      const box = $("log");
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
      for (const rec of r.logs) {
        logSeq = Math.max(logSeq, rec.seq);
        const t = new Date(rec.ts * 1000).toLocaleTimeString();
        const color = rec.level === "error" || rec.level === "fatal" ? "#ff8b7a"
          : rec.level === "warning" ? "#ffcf6b" : rec.level === "debug" ? "#8fa3b8" : "";
        box.insertAdjacentHTML("beforeend",
          `<span style="color:#6b7f95">${t}</span> <span style="color:${color || "#cfe0f2"}">[${esc(rec.source)}] ${esc(rec.msg)}</span>\n`);
      }
      while (box.childNodes.length > 1200) box.removeChild(box.firstChild);
      if (atBottom) box.scrollTop = box.scrollHeight;
    }
  } catch (e) { /* 忽略 */ }
}

async function trackJob(job) {
  if (!job || !job.id) { await refresh(); return; }
  busy = true; render();
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    let j;
    try { j = (await api("api/job?id=" + encodeURIComponent(job.id))).job; } catch (e) { break; }
    await pollLogs();
    if (!j || j.state !== "running") {
      busy = false;
      if (j && j.state === "failed") alert((LANG === "zh" ? "任务失败：" : "Failed: ") + j.error);
      else if (j && j.result && j.result.message) {
        $("applyHint").innerHTML = `<div class="banner ok">${esc(j.result.message)}</div>`
          + (j.result.tail ? `<pre class="log">${esc(j.result.tail.join("\n"))}</pre>` : "");
      }
      await refresh();
      return;
    }
  }
  busy = false; await refresh();
}

/* ------------------------------ 事件绑定 ------------------------------ */
function bind() {
  document.querySelectorAll(".mode").forEach((e) => e.addEventListener("click", () => setMode(e.dataset.mode, true)));
  document.querySelectorAll("[data-fill]").forEach((b) => b.addEventListener("click", () => {
    const ta = $("mirrors");
    if (!ta.value.includes(b.dataset.fill)) { ta.value = (ta.value.trim() + "\n" + b.dataset.fill).trim(); dirty = true; }
  }));
  document.querySelectorAll("[data-fill-gh]").forEach((b) => b.addEventListener("click", () => {
    const ta = $("gh-mirrors");
    if (!ta.value.includes(b.dataset.fillGh)) { ta.value = (ta.value.trim() + "\n" + b.dataset.fillGh).trim(); dirty = true; }
  }));
  document.querySelectorAll("input,select,textarea").forEach((e) => {
    if (e.id === "master" || e.id === "pullImage") return;
    e.addEventListener("input", () => { dirty = true; });
  });

  $("langBtn").onclick = () => { LANG = LANG === "zh" ? "en" : "zh"; applyI18n(); render(); };

  $("master").onchange = async () => {
    const on = $("master").checked;
    const mode = (S && S.mode) || (CFG && CFG.mode) || "docker_mirror";
    if (on && (mode === "upstream_proxy" || mode === "wireguard") && !confirm(T("confirm_restart"))) {
      $("master").checked = false;
      return;
    }
    dirty = false;
    try { await trackJob((await api("api/toggle", { enabled: on })).job); }
    catch (e) { alert(e.message); await refresh(); }
  };

  $("btnSave").onclick = async () => {
    try { CFG = (await api("api/config", { config: readForm() })).config; dirty = false; $("applyHint").innerHTML = `<div class="banner ok">${T("saved")}</div>`; await refresh(); }
    catch (e) { alert(e.message); }
  };

  $("btnApply").onclick = async () => {
    const c = readForm();
    const needRestart = c.enabled && (c.mode === "upstream_proxy" || c.mode === "wireguard");
    if (needRestart && !confirm(T("confirm_restart"))) return;
    try { await trackJob((await api("api/apply", { config: c, enabled: c.enabled })).job); }
    catch (e) { alert(e.message); await refresh(); }
  };

  $("btnRollback").onclick = async () => {
    if (!confirm(T("confirm_rollback"))) return;
    try { await trackJob((await api("api/rollback", {})).job); } catch (e) { alert(e.message); }
  };

  $("btnInstall").onclick = async () => {
    try { await trackJob((await api("api/kernel/install", { url: $("kernel-url").value.trim() })).job); }
    catch (e) { alert(e.message); }
  };

  $("btnKernelRestart").onclick = async () => {
    try { await trackJob((await api("api/kernel/restart", {})).job); } catch (e) { alert(e.message); }
  };

  $("btnTest").onclick = async () => {
    $("testOut").innerHTML = `<div class="banner ok">${LANG === "zh" ? "正在测试…" : "Testing…"}</div>`;
    try {
      const r = await api("api/test", {});
      await waitAndRenderTest(r.job);
    } catch (e) { alert(e.message); }
  };

  $("btnPullTest").onclick = async () => {
    const image = $("pullImage").value.trim() || "hello-world:latest";
    try { await trackJob((await api("api/pull-test", { image })).job); } catch (e) { alert(e.message); }
  };

  $("btnPauseLog").onclick = () => { paused = !paused; $("btnPauseLog").textContent = (paused ? "▶ " : "") + T("btn_pause"); };
  $("btnClearLog").onclick = () => { $("log").textContent = ""; };

  $("wgParse").onclick = async () => {
    try {
      const r = (await api("api/wg/parse", { config: $("wg-config").value })).result;
      $("wgResult").innerHTML = r.valid
        ? `<span class="v-ok">✓ endpoint ${esc(r.endpoint)} · ip ${esc(r.ip)} · mtu ${esc(r.mtu)} · ${r.peer_count} peer · allowed ${esc((r.allowed_ips || []).join(","))}</span>`
        : `<span class="v-bad">✗ ${esc(r.error)}</span>`;
    } catch (e) { $("wgResult").textContent = e.message; }
  };
}

async function waitAndRenderTest(job) {
  if (!job || !job.id) return;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    const j = (await api("api/job?id=" + encodeURIComponent(job.id))).job;
    if (j && j.state !== "running") {
      if (j.state === "failed") { $("testOut").innerHTML = `<div class="banner err">${esc(j.error)}</div>`; return; }
      renderTest(j.result);
      return;
    }
  }
}

function renderTest(res) {
  if (!res) return;
  const rows = (arr) => (arr || []).map((r) =>
    `<div class="row"><span>${esc(r.name || r.url)}${r.via_proxy ? " · " + T("via_proxy") : ""}</span>
     <span class="${r.ok ? "v-ok" : "v-bad"}">${r.ok ? ("HTTP " + esc(r.status) + " · " + esc(r.ms) + "ms") : esc(r.error)}</span></div>`).join("");
  let html = "";
  if (res.kernel) html += `<div class="banner ${res.kernel.ok ? "ok" : "err"}">${LANG === "zh" ? "内核延迟测试" : "Kernel delay"}: ${res.kernel.ok ? esc(res.kernel.delay_ms) + "ms" : esc(res.kernel.error)}</div>`;
  html += `<div class="grid2"><div><h3>${T("direct")}</h3><div class="list">${rows(res.direct)}</div></div>
           <div><h3>${T("via_proxy")}</h3><div class="list">${rows(res.via_proxy)}</div></div></div>`;
  if ((res.mirrors || []).length) html += `<h3>${T("mirrors_reach")}</h3><div class="list">${rows(res.mirrors)}</div>`;
  if ((res.github || []).length) html += `<h3>${T("github_reach")}</h3><div class="list">${rows(res.github)}</div>`;
  $("testOut").innerHTML = html;
}

async function rollbackTo(id) {
  if (!confirm(T("confirm_rollback"))) return;
  try { await trackJob((await api("api/rollback", { backup_id: id })).job); } catch (e) { alert(e.message); }
}

/* 一键修复权限：关闭 Supervisor 保护模式并重启插件（保护模式是 host_pid/docker_api 的前提） */
async function selfHeal() {
  if (!confirm(T("confirm_self_heal"))) return;
  $("banners").innerHTML = `<div class="banner warn">${T("self_heal_started")}</div>`;
  try { await api("api/self-heal", {}); } catch (e) { /* 插件会立刻重启，请求中断属预期 */ }
  setTimeout(() => location.reload(), 12000);
}

applyI18n();
bind();
refresh();
setInterval(refresh, 5000);
setInterval(pollLogs, 2000);
