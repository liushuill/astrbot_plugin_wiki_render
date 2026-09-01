const bridge = window.AstrBotPluginPage;
let context = {};

async function refreshOverview() {
  const ov = await bridge.apiGet("overview");
  document.getElementById("ov-browser").textContent =
    ov.has_playwright ? (ov.browser_broken ? "⚠️ Chromium 异常" : "✅ Playwright + Chromium 可用") : "❌ 未安装 playwright";
  document.getElementById("ov-render-mode").textContent =
    ov.render_mode === "native" ? "原生渲染（去顶栏）" : "自建模板";
  document.getElementById("ov-screen").textContent =
    ov.screen_default === "portrait" ? "竖屏" : "横屏";
  document.getElementById("ov-wiki").textContent = ov.default_wiki_api || "未设置";
  const rp = ov.render_report || {};
  document.getElementById("rp-total").textContent = rp.total ?? 0;
  document.getElementById("rp-ok").textContent = rp.ok ?? 0;
  document.getElementById("rp-fail").textContent = rp.failed ?? 0;
  document.getElementById("rp-avg").textContent = rp.avg_duration ?? 0;
  const tb = document.getElementById("rp-slowest");
  tb.innerHTML = "";
  (rp.slowest || []).forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(s.page || "")}</td><td>${s.duration ?? 0}</td>`;
    tb.appendChild(tr);
  });
  const ta = document.getElementById("rp-audit");
  ta.innerHTML = "";
  const actions = { login: "登录", logout: "登出", web_login_remove: "插件页删除登录", web_binding_remove: "插件页解除绑定" };
  (ov.audit_last || []).forEach((a) => {
    const tr = document.createElement("tr");
    const ts = new Date((a.ts || 0) * 1000).toLocaleString();
    const result = a.success ? "✅" : "❌";
    tr.innerHTML = `<td class="mono">${ts}</td><td>${escapeHtml(actions[a.action] || a.action)}</td><td>${escapeHtml(a.operator || "-")}</td><td class="mono">${escapeHtml(a.target || "")}</td><td>${result}</td>`;
    ta.appendChild(tr);
  });
  if (!(ov.audit_last || []).length) {
    ta.innerHTML = '<tr><td colspan="5" class="empty">暂无审计记录</td></tr>';
  }
}

async function refreshBindings() {
  const data = await bridge.apiGet("bindings");
  const tb = document.getElementById("bd-list");
  tb.innerHTML = "";
  const list = Object.entries(data.bindings || {});
  if (!list.length) {
    tb.innerHTML = '<tr><td colspan="5" class="empty">暂无绑定</td></tr>';
    return;
  }
  for (const [sid, info] of list) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">${escapeHtml(sid)}</td>
      <td>${escapeHtml(info.site_name || "-")}</td>
      <td class="mono">${escapeHtml(info.api_url || "-")}</td>
      <td>${info.screen === "portrait" ? "竖屏" : "横屏"}</td>
      <td><button class="btn btn-danger" data-sid="${escapeHtml(sid)}">解除</button></td>`;
    tr.querySelector("button").addEventListener("click", async (e) => {
      if (!confirm(`确定解除会话 ${sid} 的绑定？`)) return;
      await bridge.apiPost("bindings/remove", { session_id: sid });
      refreshBindings();
    });
    tb.appendChild(tr);
  }
}

async function refreshLogins() {
  const data = await bridge.apiGet("logins");
  const tb = document.getElementById("lg-list");
  tb.innerHTML = "";
  const list = Object.entries(data.logins || {});
  if (!list.length) {
    tb.innerHTML = '<tr><td colspan="4" class="empty">暂无登录状态</td></tr>';
    return;
  }
  for (const [sid, info] of list) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">${escapeHtml(sid)}</td>
      <td>${escapeHtml(info.username || "-")}</td>
      <td>${new Date((info.logged_in_at || 0) * 1000).toLocaleString()}</td>
      <td><button class="btn btn-danger" data-sid="${escapeHtml(sid)}">登出</button></td>`;
    tr.querySelector("button").addEventListener("click", async () => {
      await bridge.apiPost("logins/remove", { session_id: sid });
      refreshLogins();
    });
    tb.appendChild(tr);
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function switchPage(name) {
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === name));
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${name}`));
  const titles = { overview: "概览", bindings: "绑定管理", logins: "登录状态", settings: "设置" };
  document.getElementById("page-title").textContent = titles[name] || name;
  if (name === "overview") refreshOverview();
  if (name === "bindings") refreshBindings();
  if (name === "logins") refreshLogins();
  if (name === "settings") refreshSettings();
}

async function refreshSettings() {
  const data = await bridge.apiGet("settings/arrays");
  document.getElementById("set-ns-excludes").value = (data.random_namespace_excludes || []).join("\n");
  document.getElementById("set-allowlist").value = (data.allowed_wiki_apis || []).join("\n");
  document.getElementById("save-arrays-msg").textContent = "";
}

async function saveArrays() {
  const nsText = document.getElementById("set-ns-excludes").value;
  const allowText = document.getElementById("set-allowlist").value;
  const ns = nsText.split(/[\r\n]+/).map(s => s.trim()).filter(Boolean);
  const allow = allowText.split(/[\r\n]+/).map(s => s.trim()).filter(Boolean);
  const msg = document.getElementById("save-arrays-msg");
  try {
    if (ns.length) await bridge.apiPost("settings/arrays", { key: "random_namespace_excludes", value: ns });
    else await bridge.apiPost("settings/arrays", { key: "random_namespace_excludes", value: [] });
    if (allow.length) await bridge.apiPost("settings/arrays", { key: "allowed_wiki_apis", value: allow });
    else await bridge.apiPost("settings/arrays", { key: "allowed_wiki_apis", value: [] });
    msg.textContent = "✅ 已保存";
    msg.style.color = "#16a34a";
  } catch (e) {
    msg.textContent = "❌ 保存失败：" + (e.message || e);
    msg.style.color = "#ef4444";
  }
}

async function init() {
  context = await bridge.ready();
  document.querySelectorAll(".nav-item").forEach((n) =>
    n.addEventListener("click", () => switchPage(n.dataset.page))
  );
  document.getElementById("btn-save-arrays").addEventListener("click", saveArrays);
  document.getElementById("btn-refresh").addEventListener("click", () => {
    const active = document.querySelector(".nav-item.active")?.dataset.page || "overview";
    switchPage(active);
  });
  switchPage("overview");
}

init();
