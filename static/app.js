/* 里程归仓工具前端。示意线 + 表格，不接地图服务。 */
"use strict";

const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)",
                "var(--series-5)", "var(--series-6)", "var(--series-7)", "var(--series-8)"];
const STATUS_COLOR = { located: "var(--st-good)", ambiguous: "var(--st-warning)", exception: "var(--st-critical)" };
const STATUS_NAME = { located: "已入仓", ambiguous: "待消歧", exception: "异常" };
const VER_STATUS = { draft: "草稿", published: "已发布", superseded: "已取代" };

const state = { route: null, versions: [], selectedVersion: null, records: [] };

const $ = (sel) => document.querySelector(sel);
const fmt = (x) => (x === null || x === undefined) ? "—" : Number(x).toFixed(3).replace(/\.?0+$/, "");

async function api(path, opts = {}) {
  const res = await fetch(path, opts.body !== undefined
    ? { method: opts.method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts.body) }
    : opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2600);
}

/* ---------------------------------------------------------------- 初始化与路由 */

async function loadRoutes() {
  const routes = await api("/api/routes");
  const sel = $("#routeSelect");
  sel.innerHTML = routes.map(r => `<option>${r}</option>`).join("") + `<option value="__new__">＋ 新路线…</option>`;
  if (!state.route && routes.length) state.route = routes[0];
  if (state.route && !routes.includes(state.route)) state.route = routes[0] || null;
  sel.value = state.route || "__new__";
  toggleNewRouteInput();
}

function toggleNewRouteInput() {
  let inp = $("#newRouteName");
  if ($("#routeSelect").value === "__new__") {
    if (!inp) {
      inp = document.createElement("input");
      inp.id = "newRouteName"; inp.placeholder = "新路线编号，如 G108";
      $("#routeSelect").after(inp);
    }
    inp.oninput = () => { state.route = inp.value.trim(); };
    state.route = inp.value.trim();
  } else {
    if (inp) inp.remove();
    state.route = $("#routeSelect").value;
  }
}

async function reload() {
  if (!state.route) { renderAll(); return; }
  state.versions = await api(`/api/versions?route=${encodeURIComponent(state.route)}`);
  state.records = await api(`/api/records?route=${encodeURIComponent(state.route)}`);
  renderAll();
}

function renderAll() {
  renderVersionTable();
  renderVersionDetail();
  renderRecords();
  renderReverseOptions();
  loadFacts();
}

/* ---------------------------------------------------------------- 版本与断链 */

function renderVersionTable() {
  const t = $("#versionTable");
  if (!state.versions.length) {
    t.innerHTML = `<tr><td class="muted">暂无版本，请在下方新建。</td></tr>`;
    return;
  }
  t.innerHTML = `<tr><th>版本</th><th>生效日期</th><th>状态</th><th></th></tr>` +
    state.versions.map(v => `
      <tr class="clickable ${v.id === state.selectedVersion ? "selected" : ""}" data-vid="${v.id}">
        <td>V${v.version_no}</td><td>${v.effective_date}</td>
        <td><span class="badge ${v.status}">${VER_STATUS[v.status]}</span></td>
        <td>${v.status === "draft" ? `<button class="danger" data-del="${v.id}">删除</button>` : ""}</td>
      </tr>`).join("");
  t.querySelectorAll("tr.clickable").forEach(tr =>
    tr.onclick = () => { state.selectedVersion = Number(tr.dataset.vid); renderVersionTable(); renderVersionDetail(); });
  t.querySelectorAll("[data-del]").forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    if (!confirm("删除该草稿版本？")) return;
    await api(`/api/versions/${b.dataset.del}`, { method: "DELETE" });
    state.selectedVersion = null;
    await reload(); toast("草稿已删除");
  });
}

function breakInfo(segs, i) {
  const a = segs[i], b = segs[i + 1];
  const delta = b.read_start - a.read_end;
  if (Math.abs(delta) < 1e-9) return { kind: "连续", text: "连续" };
  if (delta > 0) {
    // 跳过的读数若被其它段（如逆向段）覆盖，则只是编号跳变，不是缺号
    const lo = a.read_end, hi = b.read_start;
    const covered = segs.some(s => Math.min(s.read_start, s.read_end) <= lo + 1e-9
                                && Math.max(s.read_start, s.read_end) >= hi - 1e-9);
    return covered
      ? { kind: "跳变", text: `读数跳变 ${fmt(a.read_end)}→${fmt(b.read_start)}` }
      : { kind: "短链", text: `短链 缺号(${fmt(a.read_end)}, ${fmt(b.read_start)})` };
  }
  return { kind: "长链", text: `长链 重号[${fmt(b.read_start)}, ${fmt(a.read_end)}]` };
}

function renderVersionDetail() {
  const box = $("#versionDetail");
  const v = state.versions.find(x => x.id === state.selectedVersion);
  if (!v) { box.innerHTML = `<p class="muted">选择左侧版本查看断链示意与区段映射。</p>`; return; }
  const segs = v.segments;
  const isDraft = v.status === "draft";
  const recs = state.records.filter(r => r.version_id === v.id);

  let html = `<h2>V${v.version_no} · ${v.route} · 生效 ${v.effective_date}
    <span class="badge ${v.status}">${VER_STATUS[v.status]}</span></h2>
    <p class="muted">${v.note || ""}</p>`;
  html += segs.length ? schematicSVG(v, recs) : `<p class="muted">尚未定义区段。</p>`;
  html += `<div class="legend">${segs.map((s, i) =>
    `<span><i class="dot" style="background:${SERIES[i % 8]}"></i>${s.label}</span>`).join("")}
    <span><i class="dot st-located"></i>已入仓记录</span>
    <span><i class="dot st-ambiguous"></i>歧义候选</span></div>`;

  // 区段表（草稿可编辑）
  html += `<h3>分段映射（读数 → 连续位置）</h3><table class="tbl" id="segTable"><tr>
    <th>区段标记</th><th>读数起</th><th>读数讫</th><th>链位起</th><th>链位讫</th><th>与下一段</th>${isDraft ? "<th></th>" : ""}</tr>`;
  segs.forEach((s, i) => {
    const brk = i < segs.length - 1 ? breakInfo(segs, i).text : "—";
    html += isDraft ? `<tr>
      <td><input class="lbl" data-f="label" data-i="${i}" value="${s.label}"></td>
      <td><input type="number" step="0.001" data-f="read_start" data-i="${i}" value="${s.read_start}"></td>
      <td><input type="number" step="0.001" data-f="read_end" data-i="${i}" value="${s.read_end}"></td>
      <td>${fmt(s.chain_start)}</td><td>${fmt(s.chain_end)}</td><td>${brk}</td>
      <td><button data-rmseg="${i}">移除</button></td></tr>`
      : `<tr><td>${s.label}</td><td>${fmt(s.read_start)}</td><td>${fmt(s.read_end)}</td>
      <td>${fmt(s.chain_start)}</td><td>${fmt(s.chain_end)}</td><td>${brk}</td></tr>`;
  });
  html += `</table>`;

  if (isDraft) {
    html += `<div class="row">
      <button id="btnAddSeg">＋ 加一段</button>
      <button id="btnSaveSeg" class="primary">保存区段</button>
      <button id="btnPreview">预览受影响记录</button>
      <button id="btnPublish" class="primary">发布此版本</button>
    </div><div id="previewWrap"></div>`;
  }
  box.innerHTML = html;

  if (isDraft) wireDraftEditor(v);
}

function wireDraftEditor(v) {
  const readRows = () => {
    const rows = {};
    $("#segTable").querySelectorAll("input").forEach(inp => {
      const i = inp.dataset.i;
      rows[i] = rows[i] || {};
      rows[i][inp.dataset.f] = inp.dataset.f === "label" ? inp.value : Number(inp.value);
    });
    return Object.keys(rows).sort().map(k => rows[k]);
  };
  $("#btnAddSeg").onclick = async () => {
    const rows = readRows();
    const last = rows[rows.length - 1];
    rows.push({ label: `区段${rows.length + 1}`, read_start: last ? last.read_end : 0, read_end: (last ? last.read_end : 0) + 1 });
    await saveSegs(v.id, rows);
  };
  $("#segTable").querySelectorAll("[data-rmseg]").forEach(b => b.onclick = async () => {
    const rows = readRows(); rows.splice(Number(b.dataset.rmseg), 1);
    await saveSegs(v.id, rows);
  });
  $("#btnSaveSeg").onclick = async () => {
    try { await saveSegs(v.id, readRows()); toast("区段已保存"); }
    catch (e) { toast(e.message); }
  };
  $("#btnPreview").onclick = async () => {
    try {
      await saveSegs(v.id, readRows());
      const changes = await api(`/api/versions/${v.id}/preview`);
      renderPreview(changes);
    } catch (e) { toast(e.message); }
  };
  $("#btnPublish").onclick = async () => {
    if (!confirm("发布后区段不可再改，受影响记录将按新版重算，旧公里事实撤回重建。确认发布？")) return;
    try {
      await saveSegs(v.id, readRows());
      const r = await api(`/api/versions/${v.id}/publish`, { body: {} });
      toast(`已发布，影响 ${r.affected} 条记录`);
      await reload();
    } catch (e) { toast(e.message); }
  };
}

async function saveSegs(vid, rows) {
  await api(`/api/versions/${vid}/segments`, { method: "PUT", body: { segments: rows } });
  await reload();
}

function renderPreview(changes) {
  const w = $("#previewWrap");
  if (!changes.length) { w.innerHTML = `<p class="muted">预览：没有记录受到影响。</p>`; return; }
  w.innerHTML = `<h3>预览：${changes.length} 条记录将变化</h3>
    <table class="tbl"><tr><th>记录</th><th>发生日期</th><th>读数</th>
    <th>现状</th><th>发布后</th></tr>` +
    changes.map(c => `<tr>
      <td>#${c.record_id}</td><td>${c.occurred_date}</td><td>${fmt(c.reading)}</td>
      <td>${STATUS_NAME[c.old_status] || c.old_status}${c.old_chain !== null ? " @ " + fmt(c.old_chain) : ""}</td>
      <td class="preview-changed">${STATUS_NAME[c.new_status] || c.new_status}${c.new_chain !== null ? " @ " + fmt(c.new_chain) : ""}</td>
    </tr>`).join("") + `</table>`;
}

/* ---------------------------------------------------------------- SVG 示意 */

function schematicSVG(v, recs) {
  const segs = v.segments;
  if (!segs.length) return "";
  const total = segs[segs.length - 1].chain_end;
  const W = 960, ml = 70, mr = 30;
  const sx = (W - ml - mr) / (total || 1);
  const X = (c) => ml + c * sx;
  const laneH = 52, top = 46;
  const stripY = top + segs.length * laneH + 26;
  const H = stripY + 46;

  // 刻度：约每 2km 一格
  const step = Math.max(1, Math.round(total / 10));
  let ticks = "";
  for (let c = 0; c <= total + 1e-9; c += step) {
    ticks += `<line x1="${X(c)}" y1="${top - 14}" x2="${X(c)}" y2="${top - 8}" stroke="var(--baseline)"/>
      <text x="${X(c)}" y="${top - 20}" text-anchor="middle" font-size="10" fill="var(--muted)">${fmt(c)}</text>`;
  }

  let body = "";
  segs.forEach((s, i) => {
    const y = top + i * laneH;
    const color = SERIES[i % 8];
    body += `<rect x="${X(s.chain_start)}" y="${y}" width="${Math.max(2, (s.chain_end - s.chain_start) * sx)}"
      height="16" rx="4" fill="${color}"><title>${s.label}：读数 ${fmt(s.read_start)}→${fmt(s.read_end)}，链位 ${fmt(s.chain_start)}→${fmt(s.chain_end)}</title></rect>`;
    // 读数标签：起端在左、讫端在右
    body += `<text x="${X(s.chain_start)}" y="${y - 5}" font-size="11" fill="var(--text-secondary)">读数 ${fmt(s.read_start)}</text>
      <text x="${X(s.chain_end)}" y="${y - 5}" text-anchor="end" font-size="11" fill="var(--text-secondary)">${fmt(s.read_end)}</text>`;
    // 读数递增方向箭头
    const ax = s.read_end > s.read_start ? X(s.chain_end) - 6 : X(s.chain_start) + 6;
    const dir = s.read_end > s.read_start ? 1 : -1;
    body += `<path d="M${ax} ${y + 8} l${-6 * dir} -4 v8 z" fill="#fff"/>`;
    if (s.read_end < s.read_start)
      body += `<text x="${X(s.chain_start) + 10}" y="${y + 34}" font-size="10" fill="var(--series-2)">逆向读数</text>`;
    // 与下一段的断链连接
    if (i < segs.length - 1) {
      const brk = breakInfo(segs, i);
      const x = X(s.chain_end), ny = top + (i + 1) * laneH;
      const dashed = brk.kind !== "连续" ? `stroke-dasharray="4 3"` : "";
      const cColor = brk.kind === "短链" ? "var(--st-critical)" : brk.kind === "长链" ? "var(--st-warning)" : "var(--baseline)";
      body += `<line x1="${x}" y1="${y + 16}" x2="${x}" y2="${ny}" stroke="${cColor}" ${dashed} stroke-width="2"/>
        <text x="${x + 6}" y="${(y + 16 + ny) / 2 + 4}" font-size="11" fill="${cColor}">${brk.text}</text>`;
    }
  });

  // 记录条带：已入仓实心点、歧义候选空心点
  let dots = "";
  recs.forEach((r, idx) => {
    const y = stripY + (idx % 3) * 12;
    (r.placements || []).forEach(p => {
      const chosen = p.chosen === 1;
      const fill = chosen ? STATUS_COLOR.located : "none";
      const stroke = chosen ? STATUS_COLOR.located : STATUS_COLOR.ambiguous;
      dots += `<circle cx="${X(p.chain_pos)}" cy="${y}" r="5" fill="${fill}" stroke="${stroke}" stroke-width="2">
        <title>#${r.id} ${r.occurred_date} 读数${fmt(r.reading)} → 链位 ${fmt(p.chain_pos)}（${STATUS_NAME[r.status]}）</title></circle>`;
    });
  });

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="版本区段示意">
    <line x1="${ml}" y1="${top - 12}" x2="${W - mr}" y2="${top - 12}" stroke="var(--baseline)"/>
    <text x="${ml - 6}" y="${top - 16}" text-anchor="end" font-size="10" fill="var(--muted)">链位km</text>
    ${ticks}${body}
    <text x="${ml - 6}" y="${stripY + 4}" text-anchor="end" font-size="10" fill="var(--muted)">记录</text>
    <line x1="${ml}" y1="${stripY + 6}" x2="${W - mr}" y2="${stripY + 6}" stroke="var(--gridline)"/>
    ${dots}</svg>`;
}

/* ---------------------------------------------------------------- 数据导入 */

async function doImport() {
  const text = $("#csvInput").value.trim();
  if (!text) { toast("请粘贴 CSV 数据"); return; }
  try {
    const r = await api("/api/records/import", { body: { csv_text: text, source: "页面导入" } });
    const s = r.summary;
    $("#importSummary").innerHTML =
      `共 ${s.total} 条：<span class="badge located">已入仓 ${s.located || 0}</span>
       <span class="badge ambiguous">待消歧 ${s.ambiguous || 0}</span>
       <span class="badge exception">异常 ${s.exception || 0}</span>`;
    $("#importResult").innerHTML = `<table class="tbl"><tr><th>记录</th><th>状态</th><th>候选链位</th><th>说明</th></tr>` +
      r.results.map(x => `<tr><td>#${x.record_id}</td>
        <td><span class="badge ${x.status}">${STATUS_NAME[x.status]}</span></td>
        <td>${x.candidates.map(c => fmt(c.chain)).join(" / ") || "—"}</td>
        <td class="muted">${x.reason || ""}</td></tr>`).join("") + `</table>`;
    await reload();
  } catch (e) { toast(e.message); }
}

/* ---------------------------------------------------------------- 记录与歧义 */

function renderRecords() {
  const filter = $("#recStatusFilter").value;
  const recs = state.records.filter(r => !filter || r.status === filter);
  if (!recs.length) { $("#recordsWrap").innerHTML = `<p class="muted">没有记录。</p>`; return; }
  $("#recordsWrap").innerHTML = `<table class="tbl"><tr>
    <th>#</th><th>方向</th><th>读数</th><th>发生日期</th><th>区段标记</th><th>版本</th><th>状态</th><th>链位 / 候选</th></tr>` +
    recs.map(r => {
      let cell;
      if (r.status === "ambiguous") {
        cell = r.placements.map(p => `<label>
          <input type="radio" name="res${r.id}" value="${p.id}" ${p.chosen ? "checked" : ""}>
          ${fmt(p.chain_pos)}（${p.segment_label || "?"}）</label>`).join(" ") +
          ` <button data-resolve="${r.id}">确定入仓</button>`;
      } else if (r.status === "located") {
        const p = (r.placements || []).find(x => x.chosen === 1) || (r.placements || [])[0];
        cell = p ? `${fmt(p.chain_pos)}（${p.segment_label || "?"}）` : "—";
      } else {
        cell = `<span class="muted">${r.exception_reason || ""}</span>`;
      }
      return `<tr><td>${r.id}</td><td>${r.direction}</td><td>${fmt(r.reading)}</td>
        <td>${r.occurred_date}</td><td>${r.marker || "—"}</td>
        <td>${r.version_no ? "V" + r.version_no : "—"}</td>
        <td><span class="badge ${r.status}">${STATUS_NAME[r.status]}</span></td><td>${cell}</td></tr>`;
    }).join("") + `</table>`;
  $("#recordsWrap").querySelectorAll("[data-resolve]").forEach(b => b.onclick = async () => {
    const rid = b.dataset.resolve;
    const sel = document.querySelector(`input[name="res${rid}"]:checked`);
    if (!sel) { toast("请先选择一个候选位置"); return; }
    try {
      await api(`/api/records/${rid}/resolve`, { body: { placement_id: Number(sel.value) } });
      toast("已入仓"); await reload();
    } catch (e) { toast(e.message); }
  });
}

/* ---------------------------------------------------------------- 反查 */

function renderReverseOptions() {
  const sel = $("#revVersion");
  sel.innerHTML = state.versions.filter(v => v.status === "published")
    .map(v => `<option value="${v.id}">V${v.version_no}（${v.effective_date} 起）</option>`).join("");
}

async function doReverse() {
  const vid = $("#revVersion").value, chain = $("#revChain").value;
  if (!vid) { toast("没有已发布版本"); return; }
  const r = await api(`/api/reverse?version_id=${vid}&chain=${chain}`);
  $("#reverseResult").innerHTML = r.exception
    ? `<p><span class="badge exception">异常</span> ${r.exception}</p>`
    : `<p>链位 ${fmt(r.chain)} 对应读数：</p><table class="tbl"><tr><th>读数</th><th>来源区段</th></tr>` +
      r.readings.map(h => `<tr><td>${fmt(h.reading)}</td><td>${h.labels.join("、")}</td></tr>`).join("") +
      `</table>${r.readings.length > 1 ? '<p class="muted">短链断点：同一连续位置对应两个读数。</p>' : ""}`;
}

/* ---------------------------------------------------------------- 公里事实 */

async function loadFacts() {
  if (!state.route) return;
  const showRetracted = $("#factShowRetracted").checked;
  const r = await api(`/api/facts?route=${encodeURIComponent(state.route)}&include_retracted=${showRetracted}`);
  $("#factBins").innerHTML = r.active_bins.length
    ? `<table class="tbl"><tr><th>公里区间</th><th>记录数</th></tr>` +
      r.active_bins.map(b => `<tr><td>K${b.bin_km}–K${b.bin_km + 1}</td><td>${b.count}</td></tr>`).join("") + `</table>`
    : `<p class="muted">暂无有效事实。</p>`;
  $("#factLedger").innerHTML = `<table class="tbl"><tr><th>#</th><th>记录</th><th>读数</th><th>区间</th>
    <th>链位</th><th>状态</th><th>撤回时间</th></tr>` +
    r.facts.map(f => `<tr><td>${f.id}</td><td>${f.record_id}</td><td>${fmt(f.reading)}</td>
      <td>K${f.bin_km}</td><td>${fmt(f.chain_pos)}</td>
      <td><span class="badge ${f.status === "active" ? "located" : "retracted"}">${f.status === "active" ? "有效" : "已撤回"}</span></td>
      <td class="muted">${f.retracted_at || "—"}</td></tr>`).join("") + `</table>`;
}

/* ---------------------------------------------------------------- 事件绑定 */

document.querySelectorAll(".tab").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tabpane").forEach(p => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
});
$("#routeSelect").onchange = async () => { toggleNewRouteInput(); state.selectedVersion = null; await reload(); };
$("#btnCreateVersion").onclick = async () => {
  const date = $("#newVerDate").value;
  if (!state.route) { toast("请先填写路线编号"); return; }
  if (!date) { toast("请选择生效日期"); return; }
  const copy = $("#newVerCopy").value;
  try {
    const r = await api("/api/versions", { body: {
      route: state.route, effective_date: date, note: $("#newVerNote").value,
      copy_from: copy ? Number(copy) : null } });
    state.selectedVersion = r.id;
    await reload(); toast("草稿已创建，请编辑区段");
  } catch (e) { toast(e.message); }
};
$("#btnImport").onclick = doImport;
$("#btnReloadRecords").onclick = reload;
$("#recStatusFilter").onchange = renderRecords;
$("#btnReverse").onclick = () => doReverse().catch(e => toast(e.message));
$("#btnReloadFacts").onclick = loadFacts;
$("#factShowRetracted").onchange = loadFacts;

function fillCopyOptions() {
  $("#newVerCopy").innerHTML = `<option value="">空白（自行定义区段）</option>` +
    state.versions.map(v => `<option value="${v.id}">复制 V${v.version_no}（${v.effective_date}）</option>`).join("");
}
const origRenderVersionTable = renderVersionTable;
renderVersionTable = function () { origRenderVersionTable(); fillCopyOptions(); };

(async function init() {
  await loadRoutes();
  await reload();
})();
