"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  models: ["设备型号", "查看厂商、设备型号与对应的配置版本。", "/api/v1/device-models"],
  profiles: ["配置版本", "查看草稿与已发布配置，检查点位、能力绑定和生成命令。", "/api/v1/profiles"],
  capabilities: ["能力目录", "全局能力可复用于不同设备与协议。", "/api/v1/capabilities"],
  devices: ["设备测试", "查看设备实例、连接配置与可选测试能力。", "/api/v1/devices"],
};
const state = { view: "models", data: {}, model: null, revision: 0, example: false };
const escape = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const json = (value) => `<pre>${escape(JSON.stringify(value, null, 2))}</pre>`;
const badge = (value) => `<span class="badge ${["draft", "invalid", "pending"].includes(value) ? "warn" : ""}">${escape(({released:"已发布", draft:"草稿", active:"启用", valid:"配置有效", invalid:"配置无效", pending:"待校验"})[value] || value)}</span>`;
async function api(path, method = "GET") {
  if (state.example && path !== "/health") {
    if (!(path in exampleRoutes)) throw new Error("此操作没有内置示例。");
    return structuredClone(exampleRoutes[path]);
  }
  const response = await fetch(path, { method, signal: AbortSignal.timeout(20000), headers: {Accept: "application/json"} });
  if (!response.ok) {
    let message = `请求失败（${response.status}），请检查服务和数据库连接。`;
    try { const body = await response.json(); message = body.message || (typeof body.detail === "string" ? body.detail : message); } catch { /* Non-JSON server errors use the status above. */ }
    throw new Error(message);
  }
  return response.json();
}
function notice(message = "") { $("notice").hidden = !message; $("notice").textContent = message; }
function table(headers, rows) {
  if (!rows.length) return '<p class="empty">暂无记录。可通过 API 文档创建配置，或在文档审核中导入草稿。</p>';
  return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${escape(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(c=>`<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function action(kind, id, text) { return `<button class="text-button" data-action="${kind}" data-id="${id}">${text} →</button>`; }
function render() {
  const view = state.view;
  $("breadcrumb").textContent = $("view-title").textContent = labels[view][0];
  $("view-description").textContent = labels[view][1] + (state.model ? ` 当前型号：${state.model.model_name}` : "");
  document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("active", b.dataset.view === view));
  const data = state.data[view];
  if (!data) { $("content").innerHTML = '<p class="empty">此区域数据加载失败，请点击刷新重试。</p>'; return; }
  const query = $("search").value.trim().toLowerCase();
  const rows = data.filter(row=>(!state.model || row.device_model_id === state.model.id) && JSON.stringify(row).toLowerCase().includes(query));
  if (view === "models") $("content").innerHTML = table(["设备型号", "厂商", "型号编码", "状态", "操作"], rows.map(r=>[
    `<strong>${escape(r.model_name)}</strong><small>ID · ${r.id}</small>`,escape(r.vendor_name),`<span class="code">${escape(r.model_code)}</span>`,badge(r.status),action("model",r.id,"查看配置")
  ]));
  if (view === "profiles") $("content").innerHTML = table(["配置名称", "版本", "所属型号", "状态", "操作"], rows.map(r=>[
    `<strong>${escape(r.profile_name || `Profile #${r.id}`)}</strong><small>ID · ${r.id}${r.is_default ? " · 默认版本" : ""}</small>`,escape(r.version),escape(state.data.models?.find(m=>m.id===r.device_model_id)?.model_name || `#${r.device_model_id}`),badge(r.status),action("profile",r.id,"查看详情")
  ]));
  if (view === "capabilities") $("content").innerHTML = table(["能力名称", "能力编码", "类型", "说明"], rows.map(r=>[
    `<strong>${escape(r.name)}</strong>`,`<span class="code">${escape(r.code)}</span>`,escape(r.capability_kind),escape(r.description)
  ]));
  if (view === "devices") $("content").innerHTML = table(["设备实例", "实例编码", "位置", "状态", "操作"], rows.map(r=>[
    `<strong>${escape(r.instance_name)}</strong><small>ID · ${r.id}</small>`,`<span class="code">${escape(r.instance_code)}</span>`,escape(r.location),badge(r.status),action("device",r.id,"查看测试")
  ]));
}
function switchView(view) {
  state.view = view; state.model = null; state.revision++;
  $("search").value = ""; $("detail").hidden = true; render();
}
async function refresh() {
  $("refresh").disabled = $("example").disabled = true;
  notice(); state.revision++; $("detail").hidden = true;
  try {
    const entries = Object.entries(labels);
    const results = await Promise.allSettled(entries.map(([, v])=>api(v[2])));
    const failures = [];
    results.forEach((result, i)=>{
      const [key, value] = entries[i];
      state.data[key] = result.status === "fulfilled" ? result.value : null;
      if (result.status === "rejected") failures.push(`${value[0]}：${result.reason.message}`);
    });
    $("stats").innerHTML = entries.map(([key,v], i)=>`<div class="stat"><span>${v[0]}</span><i>${["◈","▤","◇","⌁"][i]}</i><strong>${state.data[key]?.length ?? "—"}</strong><small>${["已录入型号", "草稿与发布版本", "可复用基础能力", "已登记实例"][i]}</small></div>`).join("");
    notice(failures.join("\n")); render();
    try { await api("/health"); $("health").textContent = "● API 服务正常"; $("health").className = "badge"; }
    catch { $("health").textContent = "API 暂不可用"; $("health").className = "badge warn"; }
  } finally { $("refresh").disabled = $("example").disabled = false; }
}
async function showDetail(kind, id) {
  const revision = ++state.revision;
  const detail = $("detail"); detail.hidden = false; detail.innerHTML = '<p class="empty">正在读取详情…</p>';
  try {
    let title, body;
    if (kind === "profile") {
      const [profile, points, capabilities, commands] = await Promise.all([
        api(`/api/v1/profiles/${id}`), api(`/api/v1/profiles/${id}/points`), api(`/api/v1/profiles/${id}/capabilities`), api(`/api/v1/profiles/${id}/commands`)
      ]);
      title = `${profile.profile_name || "Profile"} · ${profile.version}`;
      body = `${badge(profile.status)}<h3>点位 · ${points.length}</h3>${table(["名称 / 编码","地址","数据类型","访问","单位"],points.map(p=>[`${escape(p.name)}<small>${escape(p.code)}</small>`,escape(p.address_text),escape(p.data_type),escape(p.access_mode),escape(p.unit)]))}
        <h3>能力与角色绑定 · ${capabilities.length}</h3>${capabilities.length ? capabilities.map(c=>`<div class="connection"><strong>${escape(c.capability_name)}</strong><span class="code"> · ${escape(c.capability_code)}</span><ul>${c.bindings.map(b=>`<li>${escape(b.role)} → ${escape(b.point_code)}</li>`).join("") || "<li>暂无绑定</li>"}</ul></div>`).join("") : '<p class="empty">暂无能力绑定</p>'}
        <h3>生成命令 · ${commands.length}</h3>${commands.length ? commands.map(c=>`<div class="connection"><strong>${escape(c.command_name)}</strong><p class="code">${escape(c.command_code)}</p>${json(c.operations)}</div>`).join("") : '<p class="empty">暂无生成命令，草稿发布后可在此查看。</p>'}`;
    } else {
      const [context, options] = await Promise.all([api(`/api/v1/devices/${id}/context`), api(`/api/v1/devices/${id}/test-options`)]);
      title = context.device.instance_name;
      body = `<p>配置版本：${escape(context.profile.version)} · ${badge(context.profile.status)}</p><h3>连接配置</h3><p>校验仅检查 JSON Schema，不代表设备在线，也不会建立设备连接。</p>${context.connections.map(c=>`<div class="connection"><strong>${escape(c.name)}</strong> ${badge(c.status)}<p>连接 ID · ${c.id} / 协议 ID · ${c.protocol_spec_id}</p><button class="primary" data-action="validate" data-id="${c.id}">校验连接配置</button><p id="result-${c.id}" class="result" role="status"></p></div>`).join("") || '<p class="empty">尚未配置连接。</p>'}<h3>可选测试能力</h3>${table(["能力","编码","关联命令"],options.capabilities.map(c=>[escape(c.name),escape(c.code),escape(c.commands.join(", ") || "—")]))}<h3>可选点位</h3>${table(["名称","编码","访问"],options.points.map(p=>[escape(p.name),escape(p.code),escape(p.access_mode)]))}`;
    }
    if (revision !== state.revision) return;
    detail.innerHTML = `<div class="section-head"><h2>${escape(title)}</h2><button class="text-button" data-action="close">收起详情 ×</button></div><div class="detail-body">${body}</div>`;
    detail.scrollIntoView({behavior:"smooth",block:"start"});
  } catch(error) {
    if (revision === state.revision) { detail.innerHTML = '<p class="empty">详情加载失败，请重试。</p>'; notice(error.message); }
  }
}
document.querySelector("nav").addEventListener("click", event=>{const b=event.target.closest("[data-view]"); if(b) switchView(b.dataset.view);});
$("refresh").addEventListener("click", refresh);
$("example").addEventListener("click", async ()=>{
  state.example = !state.example;
  $("example-banner").hidden = !state.example;
  $("example").textContent = state.example ? "返回真实数据" : "查看示例数据";
  switchView("models");
  await refresh();
});
$("search").addEventListener("input", render);
document.addEventListener("click", async event=>{
  const button = event.target.closest("[data-action]"); if (!button) return;
  const {action: kind, id} = button.dataset;
  if (kind === "model") {switchView("profiles"); state.model=state.data.models.find(m=>m.id===Number(id)); render();}
  if (kind === "profile" || kind === "device") await showDetail(kind, id);
  if (kind === "close") {state.revision++; $("detail").hidden=true;}
  if (kind === "validate") {
    button.disabled = true;
    const target = $(`result-${id}`); target.textContent = "正在校验…";
    try {const result = await api(`/api/v1/connections/${id}/validate-config`, "POST"); target.textContent = `校验结果：${result.status}${result.errors.length ? "\n" + JSON.stringify(result.errors, null, 2) : " · 配置符合 Schema"}`;}
    catch(error) {target.textContent = error.message;}
    finally {button.disabled = false;}
  }
});
refresh();
