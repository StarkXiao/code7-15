/* 药品批放行管理系统 —— 前端（原生 JS，无构建依赖） */
(() => {
"use strict";

const ROLE_NAMES = {
  operator: "操作工", production_lead: "生产班长",
  qc: "QC 检验员", qa: "QA 质保", admin: "管理员",
};
const STATUS_NAMES = {
  created: "已创建", weighing: "称量中", in_production: "生产中",
  production_done: "生产结束", qc_sampling: "QC 检验中", qc_done: "检验完成",
  pending_review: "待 QA 审核", released: "已放行", rejected: "已拒绝放行",
};
const STATUS_FLOW = ["created","weighing","in_production","production_done",
  "qc_sampling","pending_review","released","rejected"];

// ---------------- 状态 ----------------
const state = {
  user: null, token: localStorage.getItem("br_token"),
  page: "batches",            // batches | batch | deviations | audit | integrity
  batchId: null, batchTab: "ebr",
  catalog: null, batches: null, ebr: null, review: null,
  deviations: null, audit: [], integrity: null,
  modal: null, toast: null,
};

// ---------------- API ----------------
async function api(path, opts = {}) {
  const headers = {"Content-Type": "application/json"};
  if (state.token) headers.Authorization = "Bearer " + state.token;
  let res;
  try {
    res = await fetch(path, {
      method: opts.method || "GET",
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
  } catch {
    throw {message: "无法连接服务器，请确认后端已启动"};
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw {status: res.status, message: data.message || "请求失败", code: data.error};
  return data;
}

function toast(msg, isErr = false) {
  state.toast = {msg, isErr};
  render();
  setTimeout(() => { if (state.toast && state.toast.msg === msg) { state.toast = null; render(); } }, 3200);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function fmtTime(t) { return t ? esc(t).replace("T", " ").replace("Z", " UTC") : "—"; }
function num(v, d = 3) { return v == null ? "—" : Number(v).toFixed(d).replace(/\.?0+$/, ""); }
const can = (...roles) => state.user && (roles.includes(state.user.role) || state.user.role === "admin");

// ---------------- 业务动作 ----------------
async function loadMe() {
  if (!state.token) return false;
  try { state.user = await api("/api/me"); return true; }
  catch { state.token = null; localStorage.removeItem("br_token"); return false; }
}

async function login(username, password) {
  const data = await api("/api/auth/login", {method: "POST", body: {username, password}});
  state.user = data.user; state.token = data.token;
  localStorage.setItem("br_token", data.token);
  await loadAll();
}
function logout() {
  api("/api/auth/logout", {method: "POST"}).catch(() => {});
  state.user = null; state.token = null;
  localStorage.removeItem("br_token");
  state.page = "batches"; render();
}

async function loadAll() {
  state.catalog = await api("/api/catalog");
  await refreshBatches();
}
async function refreshBatches() {
  state.batches = await api("/api/batches");
}

async function openBatch(id, tab = "ebr") {
  state.page = "batch"; state.batchId = id; state.batchTab = tab;
  state.ebr = state.review = null;
  render();
  const [ebr, review] = await Promise.all([
    api(`/api/batches/${id}`), api(`/api/batches/${id}/review`),
  ]);
  state.ebr = ebr; state.review = review;
  render();
}

async function act(fn, okMsg) {
  try { await fn(); toast(okMsg); }
  catch (e) { toast(e.message, true); }
  render();
}

// ---------------- 视图：登录 ----------------
function viewLogin() {
  return `<div class="login-wrap">
    <div class="login-card">
      <h1>药品批放行管理系统</h1>
      <div class="sub">电子批记录（EBR）· 投料/工艺/QC 串联 · 异常阻断放行 · 审计链</div>
      <form id="loginForm">
        <div class="field"><label>用户名</label><input id="fUser" autocomplete="username" value="qa01"></div>
        <div class="field"><label>口令</label><input id="fPwd" type="password" autocomplete="current-password" value="Qa@12345"></div>
        <button class="btn-primary" type="submit">登 录</button>
      </form>
      <div class="hint">
        演示账号（口令见种子数据）：<br>
        <code>op01/op02</code> 操作工 · <code>pl01</code> 生产班长 ·
        <code>qc01/qc02</code> QC（录入/复核）· <code>qa01</code> QA（偏差处置与放行）
      </div>
    </div>
  </div>`;
}

// ---------------- 视图：批次列表 ----------------
function statusBadge(s) {
  return `<span class="badge ${s}">${STATUS_NAMES[s] || s}</span>`;
}

function viewBatches() {
  const bs = state.batches || [];
  const counts = {all: bs.length};
  ["released","rejected","pending_review"].forEach(k => counts[k] = bs.filter(b => b.status === k).length);
  const rows = bs.map(b => `<tr class="clickable" onclick="App.openBatch(${b.id})">
      <td><b>${esc(b.batch_no)}</b></td>
      <td>${esc(b.product_name)} <span class="muted small">${esc(b.strength)}</span></td>
      <td class="num">${b.batch_size.toLocaleString()}</td>
      <td>${statusBadge(b.status)}</td>
      <td class="num">${b.actual_yield_pct == null ? "—" : num(b.actual_yield_pct,2) + "%"}</td>
      <td class="small muted">${esc(b.created_by_name)}<br>${fmtTime(b.created_at)}</td>
    </tr>`).join("");
  return `
   <div class="cards">
     <div class="card"><div class="k">批次总数</div><div class="v brand">${counts.all}</div></div>
     <div class="card"><div class="k">待 QA 审核</div><div class="v warn">${counts.pending_review}</div></div>
     <div class="card"><div class="k">已放行</div><div class="v ok">${counts.released}</div></div>
     <div class="card"><div class="k">已拒绝放行</div><div class="v bad">${counts.rejected}</div></div>
   </div>
   <div class="panel">
     <div class="hd"><h2>生产批次</h2>
       ${can("operator","production_lead") ? `<button class="btn brand sm" onclick="App.modal('createBatch')">＋ 新建批次</button>` : ""}
     </div>
     <table><thead><tr>
       <th>批号</th><th>产品</th><th class="num">批量(片)</th><th>状态</th>
       <th class="num">实际收率</th><th>创建人/时间</th>
     </tr></thead><tbody>${rows || `<tr><td colspan="6" class="muted" style="padding:26px;text-align:center">暂无批次</td></tr>`}</tbody></table>
   </div>`;
}

// ---------------- 视图：EBR 工作台 ----------------
function blockerAlerts() {
  const r = state.review;
  if (!r) return "";
  if (r.batch.status !== "pending_review") return "";
  if (r.can_release) {
    return `<div class="alert ok"><span class="dot ok"></span>规则引擎判定通过：无阻断项，QA 可执行电子签名放行。</div>`;
  }
  const lis = r.blockers.map(b =>
    `<li><span class="badge sev-${b.severity}">${b.severity.toUpperCase()}</span>
      <span class="code" style="margin-left:8px">${b.code}</span>${esc(b.message)}</li>`).join("");
  return `<div class="alert bad"><b>⛔ 系统已阻断放行（${r.blockers.length} 项不符合）</b><ul>${lis}</ul></div>`;
}

function viewBatch() {
  if (!state.ebr || !state.review) return `<div class="panel"><div class="bd muted">加载中…</div></div>`;
  const {batch: b, weighing, steps, qc_results, deviations} = state.ebr;
  const frozen = ["pending_review","released","rejected"].includes(b.status);
  const openDev = deviations.filter(d => d.status === "open").length;

  const tabs = [
    ["ebr", "电子批记录"],
    ["review", "放行审核"],
    ["deviations", `偏差 (${deviations.length}${openDev ? `·${openDev}待处置` : ""})`],
    ["audit", "审计追踪"],
    ["integrity", "完整性校验"],
  ];
  const tabBar = `<div class="tabs">${tabs.map(([k, n]) =>
    `<button class="${state.batchTab === k ? "active" : ""}" onclick="App.tab('${k}')">${n}${
      k === "review" && state.review.blockers.length ? `<span class="tabbadge">${state.review.blockers.length}</span>` : ""}
    </button>`).join("")}</div>`;

  let body = "";
  if (state.batchTab === "ebr") body = tabEbr(b, weighing, steps, qc_results, frozen);
  else if (state.batchTab === "review") body = tabReview(b);
  else if (state.batchTab === "deviations") body = tabDeviations(deviations, b);
  else if (state.batchTab === "audit") body = tabAudit(b.id);
  else if (state.batchTab === "integrity") body = tabIntegrity(b.id);

  return `
   <button class="back-link" onclick="App.go('batches')">← 返回批次列表</button>
   <div class="page-title">
     <h1>${esc(b.batch_no)}</h1>${statusBadge(b.status)}
     <span class="muted small">${esc(state.catalog.product.name)} · ${esc(state.catalog.product.strength)} · ${b.batch_size.toLocaleString()} 片</span>
   </div>
   ${state.batchTab === "review" ? blockerAlerts() : ""}
   <div class="panel"><div class="bd">${tabBar}${body}</div></div>`;
}

function tabEbr(b, weighing, steps, qc, frozen) {
  const prod = can("operator","production_lead") && !frozen;
  const qcActive = can("qc") && b.status === "qc_sampling";
  // 状态推进按钮
  let actions = [];
  if (prod && b.status === "created")
    actions.push(["开始配料称量", "start-weighing", {}]);
  if (prod && b.status === "production_done" && false) {}
  if (can("qc") && b.status === "production_done")
    actions.push(["QC 取样开始检验", "qc/start", {}]);
  if (can("qc") && ["weighing","in_production"].includes(b.status) && false) {}

  // 投料表
  const bomMap = Object.fromEntries(state.catalog.bom.map(x => [x.material_id, x]));
  const wRows = state.catalog.bom.map(item => {
    const r = weighing.find(w => w.material_id === item.material_id);
    if (!r) return `<tr><td>${esc(item.material_code)}</td><td>${esc(item.material_name)}</td>
      <td class="num">${num(item.planned_qty)} ${item.uom}</td><td class="num muted">未投料</td>
      <td>—</td><td>—</td><td>—</td>
      <td>${prod ? `<button class="btn sm" onclick="App.modal('weigh',{materialId:${item.material_id}})">登记投料</button>` : ""}</td></tr>`;
    const pct = Math.abs(r.actual_qty - r.planned_qty) / r.planned_qty * 100;
    const oob = pct > item.tolerance_pct;
    return `<tr>
      <td>${esc(item.material_code)}</td><td>${esc(item.material_name)}</td>
      <td class="num">${num(r.planned_qty)} ${r.uom} <span class="muted small">(±${item.tolerance_pct}%)</span></td>
      <td class="num ${oob ? "tag-oos" : ""}">${num(r.actual_qty)} ${r.uom}
        <div class="small ${oob ? "tag-oos" : "muted"}">偏差 ${pct.toFixed(2)}%</div></td>
      <td class="small">${esc(r.weighed_by_name)}<br><span class="muted">${fmtTime(r.weighed_at)}</span></td>
      <td>${r.checked_by_name ? `<span class="tag-conform">${esc(r.checked_by_name)}</span><br><span class="small muted">${fmtTime(r.checked_at)}</span>`
        : `<span class="tag-oos small">待复核</span>`}</td>
      <td class="mono small">${r.record_hash.slice(0,10)}…</td>
      <td>${!r.checked_by && prod
        ? `<button class="btn sm ok" onclick="App.checkWeigh(${b.id},${r.id})">双人复核</button>`
        : `<span class="muted small">${r.checked_by_name ? "已锁定" : ""}</span>`}</td>
    </tr>`;
  }).join("");

  // 工艺步骤
  const stepHtml = state.catalog.steps.map(spec => {
    const rec = steps.find(x => x.step_no === spec.step_no) || {};
    const recParams = rec.params || [];
    const pRows = spec.params.map(p => {
      const pr = recParams.find(x => x.param_name === p.name);
      if (!pr) return `<tr><td>${esc(p.name)}</td>
        <td class="num muted">${p.is_pass_fail ? "合格/不合格" : `[${p.lower_limit ?? "−∞"}, ${p.upper_limit ?? "+∞"}] ${p.uom || ""}`}</td>
        <td class="num muted">未记录</td><td>—</td><td></td></tr>`;
      let conform = true, val;
      if (p.is_pass_fail) { val = pr.pass_fail ? "合格" : "不合格"; conform = !!pr.pass_fail; }
      else {
        val = `${num(pr.numeric_value)} ${p.uom || ""}`;
        conform = (p.lower_limit == null || pr.numeric_value >= p.lower_limit)
               && (p.upper_limit == null || pr.numeric_value <= p.upper_limit);
      }
      return `<tr><td>${esc(p.name)}</td>
        <td class="num muted small">${p.is_pass_fail ? "合格/不合格" : `[${p.lower_limit ?? "−∞"}, ${p.upper_limit ?? "+∞"}]`}</td>
        <td class="num ${conform ? "" : "tag-oos"}">${val}</td>
        <td class="${conform ? "tag-conform" : "tag-oos"}">${conform ? "符合" : "超限 OOS"}</td>
        <td class="mono small">${pr.record_hash.slice(0,10)}…</td></tr>`;
    }).join("");
    const stateLabel = rec.finished_at ? `<span class="tag-conform">已完成</span>`
      : rec.started_at ? `<span class="badge in_production">进行中</span>` : `<span class="muted">未开始</span>`;
    const canRecord = prod && rec.started_at && !rec.finished_at;
    return `<div class="panel" style="box-shadow:none;margin-bottom:12px">
      <div class="hd"><h2>步骤 ${spec.step_no} · ${esc(spec.name)}</h2>${stateLabel}
        <span class="small muted">${rec.operator_id ? "" : ""}</span>
        ${prod && !rec.started_at ? `<button class="btn sm brand" onclick="App.startStep(${b.id},${spec.step_no})">开始步骤</button>` : ""}
        ${prod && rec.started_at && !rec.finished_at ? `<button class="btn sm" onclick="App.modal('finishStep',{stepNo:${spec.step_no}})">结束步骤</button>` : ""}
      </div>
      <table><thead><tr><th>参数</th><th>规定限度</th><th class="num">实测值</th><th>判定</th><th>记录指纹</th></tr></thead>
        <tbody>${pRows}
        ${canRecord && spec.params.some(p => !recParams.some(rp => rp.param_name === p.name))
          ? `<tr><td colspan="5">
              <button class="btn sm" onclick="App.modal('param',{stepNo:${spec.step_no}})">＋ 登记参数</button>
            </td></tr>` : ""}
        ${spec.params.length === 0 ? `<tr><td colspan="5" class="muted small">该步骤为操作步骤，无在线参数（如手工配料）</td></tr>` : ""}
        </tbody></table>
      <div class="small muted" style="padding:8px 14px">开始：${fmtTime(rec.started_at)} ｜ 结束：${fmtTime(rec.finished_at)}</div>
    </div>`;
  }).join("");

  // QC 表
  const qRows = state.catalog.qc_specs.map(spec => {
    const r = qc.find(x => x.test_name === spec.test_name);
    const limit = spec.test_type === "pass_fail" ? "合格"
      : `[${spec.lower_limit ?? "−∞"}, ${spec.upper_limit ?? "+∞"}] ${spec.uom || ""}`;
    if (!r) return `<tr><td>${esc(spec.test_name)} <span class="badge sev-${spec.risk}" style="margin-left:6px">${spec.risk}</span></td>
      <td>${limit}</td><td class="num muted">未检验</td><td>—</td><td>—</td>
      <td>${qcActive ? `<button class="btn sm" onclick="App.modal('qc',{test:'${esc(spec.test_name)}',type:'${spec.test_type}'})">录入结果</button>` : ""}</td></tr>`;
    const val = spec.test_type === "pass_fail" ? (r.pass_fail ? "合格" : "不合格")
      : `${num(r.numeric_value)} ${spec.uom || ""}`;
    return `<tr>
      <td>${esc(r.test_name)} <span class="badge sev-${spec.risk}" style="margin-left:6px">${spec.risk}</span></td>
      <td class="small muted">${limit}</td>
      <td class="num ${r.result_conforms ? "" : "tag-oos"}">${val}</td>
      <td>${r.result_conforms ? '<span class="tag-conform">符合</span>' : '<span class="tag-oos">OOS</span>'}</td>
      <td class="small">${esc(r.tested_by_name)}<br><span class="muted">${fmtTime(r.tested_at)}</span></td>
      <td>${r.checked_by_name ? `<span class="tag-conform small">${esc(r.checked_by_name)}</span>`
        : `<span class="tag-oos small">待复核</span>`}</td>
      <td>${!r.checked_by && qcActive
        ? `<button class="btn sm ok" onclick="App.checkQc(${b.id},${r.id})">QC 复核</button>` : ""}</td>
    </tr>`;
  }).join("");

  // 顶部操作条
  const opBar = `<div class="row" style="margin-bottom:14px">
    ${b.status === "in_production" && can("production_lead")
      ? `<button class="btn" onclick="App.modal('finishProd')">填报收率并结束生产</button>` : ""}
    ${["qc_sampling"].includes(b.status) && can("qc")
      ? `<button class="btn brand" onclick="App.submitQc(${b.id})">提交 QA 放行审核</button>` : ""}
    ${frozen ? `<span class="small muted">🔒 批次已进入 ${STATUS_NAMES[b.status]}，电子批记录已冻结（数据库触发器级保护）</span>` : ""}
  </div>`;

  return `${opBar}
    <h3 style="margin:6px 0 10px;font-size:.95rem">① 投料记录（强制双人复核）</h3>
    <table><thead><tr><th>物料</th><th>名称</th><th class="num">理论量/公差</th>
      <th class="num">实际量</th><th>称量人/时间</th><th>复核人/时间</th><th>指纹</th><th></th>
    </tr></thead><tbody>${wRows}</tbody></table>

    <h3 style="margin:22px 0 10px;font-size:.95rem">② 工艺执行（${state.catalog.steps.length} 个步骤）</h3>
    ${stepHtml}

    <h3 style="margin:22px 0 10px;font-size:.95rem">③ QC 检验结果（录入 + 第二人复核）</h3>
    <table><thead><tr><th>检验项目</th><th>标准</th><th class="num">结果</th><th>判定</th>
      <th>检验人/时间</th><th>复核人</th><th></th></tr></thead><tbody>${qRows}</tbody></table>

    <h3 style="margin:22px 0 10px;font-size:.95rem">④ 批次元数据</h3>
    <dl class="kv">
      <dt>实际收率</dt><dd>${b.actual_yield_pct == null ? "—" : num(b.actual_yield_pct,2) + "% （窗口 " + state.catalog.yield_window.lower + "%~" + state.catalog.yield_window.upper + "%）"}</dd>
      <dt>提交时间</dt><dd>${fmtTime(b.submitted_at)}</dd>
      <dt>电子签名</dt><dd>${b.e_signature ? `<span class="mono">${esc(b.e_signature)}</span>` : "—"}</dd>
      <dt>放行意见</dt><dd>${esc(b.release_comment || "—")}</dd>
    </dl>`;
}

function tabReview(b) {
  const r = state.review;
  const counts = r.blocker_counts;
  const detailRows = r.blockers.length ? r.blockers.map(x =>
    `<tr><td><span class="badge sev-${x.severity}">${x.severity.toUpperCase()}</span></td>
      <td class="mono small">${x.code}</td><td>${esc(x.message)}</td></tr>`).join("")
    : `<tr><td colspan="3" class="tag-conform" style="padding:18px;text-align:center">✓ 全部放行条件满足</td></tr>`;

  let actions = "";
  if (b.status === "pending_review" && can("qa")) {
    if (r.can_release) {
      actions = `<div class="alert ok"><b>允许放行。</b>放行前请复核批记录与偏差档案，并重新输入口令完成电子签名（21 CFR Part 11 要求）。</div>
        <button class="btn brand" style="width:100%;padding:12px" onclick="App.modal('release')">✓ QA 电子签名放行</button>`;
    } else {
      actions = `<div class="alert bad">存在未关闭偏差或判废结论。请在「偏差」页签完成调查处置；确属不可放行的，执行拒绝。</div>
        <div class="row" style="gap:10px">
          <button class="btn" style="flex:1;padding:11px" onclick="App.modal('returnRetest')">↩ 退回补检/补录</button>
          <button class="btn danger" style="flex:2;padding:11px" onclick="App.modal('reject')">✗ 拒绝放行（记录原因）</button>
        </div>`;
    }
  }
  return `
   <div class="cards" style="grid-template-columns:repeat(4,1fr)">
     <div class="card"><div class="k">阻断项合计</div><div class="v ${r.blockers.length ? "bad" : "ok"}">${r.blockers.length}</div></div>
     <div class="card"><div class="k">Critical</div><div class="v" style="color:var(--critical)">${counts.critical}</div></div>
     <div class="card"><div class="k">Major</div><div class="v" style="color:var(--major)">${counts.major}</div></div>
     <div class="card"><div class="k">Minor</div><div class="v muted">${counts.minor}</div></div>
   </div>
   <table><thead><tr><th>等级</th><th>规则码</th><th>说明</th></tr></thead><tbody>${detailRows}</tbody></table>
   <div style="margin-top:18px">${actions}</div>
   <p class="small muted" style="margin-top:14px">规则覆盖：BOM 齐套/公差、投料双人复核、工艺步骤与参数限度、收率窗口、
     QC 齐套/双人复核/OOS、偏差闭环（open 阻断；accepted+CAPA 放行；rejected 永久阻断）。</p>`;
}

function devBadge(d) {
  if (d.status === "closed")
    return d.disposition === "accepted"
      ? `<span class="badge pill-closed">已关闭 · QA 接受</span>`
      : `<span class="badge rejected">已关闭 · 判废/退回</span>`;
  return `<span class="badge pill-open">待处置</span>`;
}

function tabDeviations(devs, b) {
  if (!devs.length) return `<div class="alert ok">无偏差记录。</div>`;
  const rows = devs.map(d => `<tr>
      <td>#${d.id}</td>
      <td><span class="badge sev-${d.severity}">${d.severity}</span></td>
      <td>${esc(d.category)}<div class="small muted">${esc(d.description)}</div></td>
      <td class="small">${esc(d.raised_by_name)}<br><span class="muted">${fmtTime(d.raised_at)}</span></td>
      <td>${devBadge(d)}
        ${d.status === "closed" ? `<div class="small muted" style="margin-top:4px">${esc(d.closed_by_name)} · ${fmtTime(d.closed_at)}</div>
          <div class="small" style="margin-top:4px">CAPA：${esc(d.capa_summary)}</div>` : ""}</td>
      <td>${d.status === "open" && can("qa")
        ? `<button class="btn sm" onclick="App.modal('closeDev',{id:${d.id}})">QA 调查处置</button>` : ""}</td>
    </tr>`).join("");
  return `<table><thead><tr><th>#</th><th>等级</th><th>描述</th><th>提出人/时间</th><th>处置</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

async function tabAuditReady(batchId) {
  const logs = await api(`/api/audit`);
  return logs.filter(l => l.batch_id === batchId);
}
function tabAudit(batchId) {
  const logs = state.audit.filter(l => l.batch_id === batchId);
  if (!logs.length && !state._auditLoaded) {
    api("/api/audit").then(ls => { state.audit = ls; state._auditLoaded = true; render(); });
    return `<div class="muted">加载审计记录…</div>`;
  }
  return `<div class="small muted" style="margin-bottom:10px">共 ${logs.length} 条，按时间顺序（哈希链见“完整性校验”页签）</div>
    <ul class="timeline">${logs.map(l => `<li class="${l.action.includes("block") || l.action === "batch_rejected" ? "blocked" : l.action === "batch_released" ? "released" : ""}">
      <span class="t-time">${fmtTime(l.ts)}</span><span class="t-action">${esc(l.action)}</span>
      <span class="small">${esc(l.actor_name)}</span>
      ${l.reason ? `<div class="small muted">${esc(l.reason)}</div>` : ""}
    </li>`).join("")}</ul>`;
}

function tabIntegrity(batchId) {
  if (!state.integrity || state.integrity._batchId !== batchId) {
    api(`/api/batches/${batchId}/integrity`).then(rep => {
      state.integrity = {...rep, _batchId: batchId}; render();
    });
    return `<div class="muted">正在重放哈希链与逐条复算记录指纹…</div>`;
  }
  const r = state.integrity;
  const chain = `<div class="alert ${r.chain.ok ? "ok" : "bad"}">
      <b>审计哈希链：${r.chain.ok ? "✓ 完整" : "✗ 断裂"}</b>
      <div class="small" style="margin-top:6px">已校验 ${r.chain.entries} 条；链尾哈希：</div>
      <div class="hashbox" style="margin-top:6px">${esc(r.chain.tail_hash || "")}</div>
      ${r.chain.reason ? `<div style="margin-top:6px">${esc(r.chain.reason)}（#${r.chain.broken_at}）</div>` : ""}
    </div>`;
  let rec = "";
  if (r.records) {
    rec = `<div class="alert ${r.records.ok ? "ok" : "bad"}">
      <b>EBR 记录指纹：${r.records.ok ? "✓ 全部一致" : `✗ ${r.records.mismatches.length} 条被篡改`}</b>
      <div class="small" style="margin-top:6px">逐条复算 ${r.records.records_checked} 条投料/工艺/QC 记录的 SHA-256</div>
      <div class="hashbox" style="margin-top:6px">批次状态指纹：${esc(r.records.state_hash)}</div>
      ${(r.records.mismatches || []).map(m => `<div style="margin-top:4px">✗ ${esc(m.entity)}#${m.id}：${esc(m.reason)}</div>`).join("")}
    </div>`;
  }
  return `${chain}${rec}
    <p class="small muted">防护层次：① 数据库触发器禁止 UPDATE/DELETE 审计日志，并在批次冻结后锁定业务记录；
    ② 每条业务记录保存 SHA-256 指纹，可随时复算；③ 审计条目以 prev_hash 串联，任何插入/删除/改写都会断链；
    ④ 即使攻击者 DROP 触发器改库，完整性校验立即暴露。</p>`;
}

// ---------------- 视图：偏差总表 / 全局审计 ----------------
function viewDeviations() {
  if (!state.deviations) { api("/api/deviations").then(d => { state.deviations = d; render(); }); return ""; }
  const open = state.deviations.filter(d => d.status === "open").length;
  const rows = state.deviations.map(d => `<tr class="clickable" onclick="App.openBatch(${d.batch_id},'deviations')">
    <td>#${d.id}</td><td><b>批次 #${d.batch_id}</b></td>
    <td><span class="badge sev-${d.severity}">${d.severity}</span></td>
    <td>${esc(d.category)}<div class="small muted">${esc(d.description.slice(0,80))}</div></td>
    <td>${devBadge(d)}</td>
    <td class="small">${esc(d.raised_by_name)}<br><span class="muted">${fmtTime(d.raised_at)}</span></td>
  </tr>`).join("");
  return `<div class="page-title"><h1>偏差与 CAPA</h1><span class="badge pill-open">${open} 待处置</span></div>
  <div class="panel"><table><thead><tr><th>#</th><th>批次</th><th>等级</th><th>描述</th><th>状态</th><th>提出</th></tr></thead>
  <tbody>${rows}</tbody></table></div>`;
}

function viewAudit() {
  if (!state._auditLoaded) { api("/api/audit").then(ls => { state.audit = ls; state._auditLoaded = true; render(); }); }
  const logs = state.audit;
  const rows = logs.map(l => `<tr ${l.batch_id ? `class="clickable" onclick="App.openBatch(${l.batch_id},'audit')"` : ""}>
    <td class="small mono">${fmtTime(l.ts)}</td>
    <td><b>${esc(l.action)}</b>${["release_blocked","batch_rejected"].includes(l.action)
      ? ' <span class="badge sev-critical">阻断/拒绝</span>'
      : l.action === "batch_released" ? ' <span class="badge released">放行</span>' : ""}</td>
    <td class="small">${esc(l.actor_name)}</td>
    <td class="small">${esc(l.entity_type)} ${l.entity_id ? "#" + esc(l.entity_id) : ""}</td>
    <td class="small muted">${l.batch_id ? "批次 #" + l.batch_id : ""}</td>
    <td class="small muted">${esc(l.reason || "")}</td>
  </tr>`).join("");
  return `<div class="page-title"><h1>审计追踪</h1>
    <button class="btn sm" onclick="App.verifyAll()">重放全局哈希链</button></div>
  <div class="panel"><table><thead><tr><th>时间(UTC)</th><th>动作</th><th>操作人</th><th>对象</th><th>批次</th><th>原因/备注</th></tr></thead>
  <tbody>${rows}</tbody></table></div>`;
}

// ---------------- 弹窗 ----------------
const MODALS = {
  createBatch: {
    title: "新建批次",
    body: () => `<div class="field"><label>批号（留空自动生成）</label><input id="mBatchNo" placeholder="如 VC260919-D"></div>
      <div class="field"><label>批量（片）</label><input id="mSize" type="number" value="${state.catalog.product.batch_size || 100000}"></div>`,
    submit: async () => {
      const no = document.getElementById("mBatchNo").value.trim();
      const size = Number(document.getElementById("mSize").value);
      const b = await api("/api/batches", {method: "POST", body: {batch_no: no || null, batch_size: size}});
      await refreshBatches(); await openBatch(b.id);
    },
  },
  weigh: {
    title: "登记投料",
    body: ({materialId}) => {
      const item = state.catalog.bom.find(x => x.material_id === materialId);
      return `<dl class="kv" style="margin-bottom:14px">
        <dt>物料</dt><dd><b>${esc(item.material_code)}</b> ${esc(item.material_name)}</dd>
        <dt>理论投料量</dt><dd>${num(item.planned_qty)} ${item.uom}（公差 ±${item.tolerance_pct}%）</dd></dl>
        <div class="field"><label>实际称量值（${item.uom}）</label><input id="mQty" type="number" step="0.001" value="${item.planned_qty}"></div>
        <div class="small muted">超出公差将自动登记偏差（major；&gt;5% 升级为 critical）。</div>`;
    },
    submit: async (p) => {
      await api(`/api/batches/${state.batchId}/weighing`, {method: "POST",
        body: {material_code: state.catalog.bom.find(x => x.material_id === p.materialId).material_code,
               actual_qty: Number(document.getElementById("mQty").value)}});
      await reloadBatch();
    },
  },
  param: {
    title: "登记工艺参数",
    body: ({stepNo}) => {
      const step = state.catalog.steps.find(s => s.step_no === stepNo);
      const recorded = new Set((state.ebr.steps.find(x => x.step_no === stepNo)?.params || []).map(p => p.param_name));
      const opts = step.params.filter(p => !recorded.has(p.name));
      return `<div class="field"><label>参数</label><select id="mParamName">
          ${opts.map(p => `<option data-pf="${p.is_pass_fail}" data-lo="${p.lower_limit ?? ""}" data-hi="${p.upper_limit ?? ""}" data-uom="${esc(p.uom || "")}">${esc(p.name)}</option>`).join("")}
        </select></div>
        <div id="mParamValueWrap"><label>实测值</label><input id="mParamVal" type="number" step="0.001"></div>
        <div class="small muted" id="mParamHint"></div>`;
    },
    afterRender: () => {
      const sel = document.getElementById("mParamName");
      const sync = () => {
        const opt = sel.selectedOptions[0];
        const wrap = document.getElementById("mParamValueWrap");
        if (opt.dataset.pf === "1") {
          wrap.innerHTML = `<label>结果</label><select id="mParamVal"><option value="true">合格</option><option value="false">不合格</option></select>`;
        } else {
          wrap.innerHTML = `<label>实测值（${opt.dataset.uom}）</label><input id="mParamVal" type="number" step="0.001">`;
        }
        document.getElementById("mParamHint").textContent =
          opt.dataset.pf === "1" ? "定性项目：不合格将直接产生 critical 偏差。"
          : `规定限度：[${opt.dataset.lo || "−∞"}, ${opt.dataset.hi || "+∞"}] ${opt.dataset.uom}`;
      };
      sel.addEventListener("change", sync); sync();
    },
    submit: async (p) => {
      const el = document.getElementById("mParamVal");
      const raw = el.value;
      const isPf = document.getElementById("mParamName").selectedOptions[0].dataset.pf === "1";
      const value = isPf ? raw === "true" : Number(raw);
      await api(`/api/batches/${state.batchId}/params`, {method: "POST",
        body: {step_no: p.stepNo, param_name: document.getElementById("mParamName").value, value}});
      await reloadBatch();
    },
  },
  qc: {
    title: "录入 QC 检验结果",
    body: ({test, type}) => {
      const spec = state.catalog.qc_specs.find(s => s.test_name === test);
      if (type === "pass_fail")
        return `<div class="field"><label>${esc(test)}（${spec.risk}）— 结果</label>
          <select id="mQcVal"><option value="true">合格</option><option value="false">不合格</option></select></div>`;
      return `<div class="field"><label>${esc(test)}（${spec.risk}）限度 [${spec.lower_limit ?? "−∞"}, ${spec.upper_limit ?? "+∞"}] ${spec.uom || ""}</label>
        <input id="mQcVal" type="number" step="0.01"></div>
        <div class="small muted">不符合将自动产生 OOS 偏差并阻断放行。</div>`;
    },
    submit: async (p) => {
      const raw = document.getElementById("mQcVal").value;
      const value = p.type === "pass_fail" ? raw === "true" : Number(raw);
      await api(`/api/batches/${state.batchId}/qc`, {method: "POST",
        body: {test_name: p.test, value}});
      await reloadBatch();
    },
  },
  finishStep: {
    title: "结束工艺步骤",
    body: ({stepNo}) => `<p>确认步骤 ${stepNo} 的全部参数已登记，结束后该步骤锁定。</p>`,
    submit: async (p) => {
      await api(`/api/batches/${state.batchId}/steps/finish`, {method: "POST", body: {step_no: p.stepNo}});
      await reloadBatch();
    },
  },
  finishProd: {
    title: "填报收率并结束生产",
    body: () => `<div class="field"><label>实际收率（%，放行窗口 ${state.catalog.yield_window.lower}~${state.catalog.yield_window.upper}）</label>
      <input id="mYield" type="number" step="0.01" value="99.2"></div>
      <div class="small muted">仅生产班长可执行；收率超限自动登记 major 偏差。</div>`,
    submit: async () => {
      await api(`/api/batches/${state.batchId}/finish-production`, {method: "POST",
        body: {actual_yield_pct: Number(document.getElementById("mYield").value)}});
      await reloadBatch();
    },
  },
  closeDev: {
    title: "QA 偏差调查与处置",
    wide: true,
    body: ({id}) => {
      const d = state.ebr.deviations.find(x => x.id === id) || state.deviations?.find(x => x.id === id);
      return `<dl class="kv" style="margin-bottom:14px">
        <dt>偏差编号</dt><dd>#${d.id} <span class="badge sev-${d.severity}">${d.severity}</span></dd>
        <dt>描述</dt><dd>${esc(d.description)}</dd></dl>
      <div class="field"><label>处置结论（决定阻断行为）</label>
        <select id="mDisposition"><option value="accepted">接受偏差（调查后继续放行，须完成 CAPA）</option>
        <option value="rejected">判废/退回（批次永久不得放行）</option></select></div>
      <div class="field"><label>调查结论与 CAPA 摘要（纠正/预防措施，至少 5 字）</label>
        <textarea id="mCapa" rows="4" placeholder="如：根因为……；纠正措施……；预防措施（培训/点检/变更）……"></textarea></div>`;
    },
    submit: async (p) => {
      await api(`/api/deviations/${p.id}/close`, {method: "POST",
        body: {disposition: document.getElementById("mDisposition").value,
              capa_summary: document.getElementById("mCapa").value}});
      await reloadBatch();
      state.deviations = null;
    },
  },
  release: {
    title: "QA 电子签名放行",
    body: () => `<div class="alert warn" style="margin-bottom:14px">本操作将使批次进入 <b>已放行</b> 终态并永久锁定记录。
        电子签名等同于手写签名，签署人承担放行责任。</div>
      <div class="field"><label>放行意见（至少 4 字）</label><textarea id="mComment" rows="3">批记录完整，偏差已闭环，符合放行标准，同意放行。</textarea></div>
      <div class="field"><label>签署人：${esc(state.user.display_name)}（${esc(state.user.username)}）— 请重新输入口令</label>
        <input id="mPwd" type="password" autocomplete="off"></div>`,
    submit: async () => {
      await api(`/api/batches/${state.batchId}/release`, {method: "POST",
        body: {comment: document.getElementById("mComment").value,
               password: document.getElementById("mPwd").value}});
      await reloadBatch(); state.batchTab = "review";
    },
  },
  reject: {
    title: "拒绝放行",
    body: () => `<div class="field"><label>拒绝原因（至少 4 字，将永久写入审计链）</label>
      <textarea id="mReason" rows="4" placeholder="如：关键检验 OOS 且偏差调查结论为不可接受……"></textarea></div>`,
    submit: async () => {
      await api(`/api/batches/${state.batchId}/reject`, {method: "POST",
        body: {reason: document.getElementById("mReason").value}});
      await reloadBatch(); state.batchTab = "review";
    },
  },
  returnRetest: {
    title: "QA 退回补检 / 补录",
    body: () => `<div class="alert warn" style="margin-bottom:14px">退回后批次解冻、状态回到「QC 检验中」；
      补录完成须重新双人复核并再次提交 QA。退回与重提事件均写入哈希审计链。</div>
      <div class="field"><label>退回原因（至少 4 字）</label>
      <textarea id="mReason" rows="4" placeholder="如：溶出度原始数据缺失，需复测并补录……"></textarea></div>`,
    submit: async () => {
      await api(`/api/batches/${state.batchId}/return-retest`, {method: "POST",
        body: {reason: document.getElementById("mReason").value}});
      await reloadBatch(); state.batchTab = "ebr";
    },
  },
};

async function reloadBatch() {
  const [ebr, review] = await Promise.all([
    api(`/api/batches/${state.batchId}`), api(`/api/batches/${state.batchId}/review`)]);
  state.ebr = ebr; state.review = review;
  await refreshBatches();
}

// ---------------- 渲染 ----------------
function renderModal() {
  if (!state.modal) return "";
  const m = MODALS[state.modal.name];
  return `<div class="modal-mask" onclick="if(event.target===this)App.closeModal()">
    <div class="modal ${m.wide ? "wide" : ""}">
      <div class="hd"><span>${m.title}</span>
        <button class="btn sm" onclick="App.closeModal()">×</button></div>
      <div class="bd">${m.body(state.modal.param || {})}</div>
      <div class="ft"><button class="btn" onclick="App.closeModal()">取消</button>
        <button class="btn brand" id="mSubmit">确认</button></div>
    </div></div>`;
}

function render() {
  const app = document.getElementById("app");
  if (!state.user) { app.innerHTML = viewLogin(); bindLogin(); return; }

  const nav = [
    ["batches", "批次与 EBR"], ["deviations", "偏差 / CAPA"], ["audit", "审计追踪"],
  ];
  let main;
  if (state.page === "batches") main = viewBatches();
  else if (state.page === "batch") main = viewBatch();
  else if (state.page === "deviations") main = viewDeviations();
  else if (state.page === "audit") main = viewAudit();

  app.innerHTML = `
    <header class="topbar">
      <div class="logo">💊 药品批放行<span>管理系统</span></div>
      <nav>${nav.map(([k, n]) => `<button class="${state.page === k ? "active" : ""}" onclick="App.go('${k}')">${n}</button>`).join("")}</nav>
      <div class="who"><b>${esc(state.user.display_name)}</b> · ${ROLE_NAMES[state.user.role] || state.user.role}
        <button onclick="App.logout()">退出</button></div>
    </header>
    <main>${main}</main>
    ${renderModal()}
    ${state.toast ? `<div class="toast ${state.toast.isErr ? "err" : ""}">${esc(state.toast.msg)}</div>` : ""}`;

  if (state.modal) {
    MODALS[state.modal.name].afterRender?.();
    document.getElementById("mSubmit").onclick = async () => {
      try {
        await MODALS[state.modal.name].submit(state.modal.param || {});
        state.modal = null; render(); toast("操作完成");
      } catch (e) { toast(e.message, true); }
    };
  }
}

function bindLogin() {
  document.getElementById("loginForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await login(document.getElementById("fUser").value.trim(),
                  document.getElementById("fPwd").value);
      render(); toast("登录成功");
    } catch (err) { toast(err.message, true); }
  };
}

// ---------------- 对外动作 ----------------
window.App = {
  async go(p) { state.page = p; state.deviations = null;
    if (p === "audit") state._auditLoaded = false;
    render();
  },
  openBatch,
  tab(k) { state.batchTab = k; state.integrity = null; render(); },
  logout,
  modal(name, param) { state.modal = {name, param}; render(); },
  closeModal() { state.modal = null; render(); },
  async checkWeigh(bid, id) {
    await act(() => api(`/api/batches/${bid}/weighing/${id}/check`, {method: "POST"}), "双人复核完成");
    await reloadBatch();
  },
  async startStep(bid, no) {
    await act(() => api(`/api/batches/${bid}/steps/start`, {method: "POST", body: {step_no: no}}), "步骤已开始");
    await reloadBatch();
  },
  async checkQc(bid, id) {
    await act(() => api(`/api/batches/${bid}/qc/${id}/check`, {method: "POST"}), "QC 复核完成");
    await reloadBatch();
  },
  async submitQc(bid) {
    await act(() => api(`/api/batches/${bid}/submit`, {method: "POST"}), "已提交 QA，EBR 进入冻结态");
    await reloadBatch(); state.batchTab = "review";
  },
  async verifyAll() {
    const rep = await api("/api/integrity");
    toast(rep.ok ? `全局哈希链校验通过（${rep.chain.entries} 条）` : "哈希链校验失败！", !rep.ok);
  },
};

// ---------------- 启动 ----------------
(async function init() {
  render();
  await loadMe();
  if (state.user) await loadAll().catch(e => toast(e.message, true));
  render();
})();
})();
