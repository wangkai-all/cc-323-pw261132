/* 项目组合分析前端 */
"use strict";

const state = {
  bootstrap: null,
  scenario: null,      // 当前打开的完整方案（含 assumptions + schedule）
  ganttView: "resource", // resource | project
  compareIds: new Set(),
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const fmtW = (v) => "W" + (Number.isInteger(v) ? v : v.toFixed(1));
const fmtNum = (v, d = 1) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));

// ---------- API ----------
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opt);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function planner() {
  return $("#planner").value.trim() || "匿名";
}

function showMessage(text, kind = "info") {
  const m = $("#message");
  m.className = kind;
  m.textContent = text;
  m.classList.remove("hidden");
  clearTimeout(showMessage._t);
  showMessage._t = setTimeout(() => m.classList.add("hidden"), 12000);
}
function hideMessage() { $("#message").classList.add("hidden"); }

// ---------- 颜色 ----------
const PALETTE = ["#2f6fed", "#7c3aed", "#0891b2", "#059669", "#d97706", "#dc2626", "#db2777", "#4f46e5"];
function projectColor(pid) {
  let h = 0;
  for (const c of String(pid)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

// ---------- 初始化 ----------
async function init() {
  state.bootstrap = await api("GET", "/api/bootstrap");
  $("#planner").value = localStorage.getItem("planner") || "";
  $("#planner").addEventListener("change", () => localStorage.setItem("planner", $("#planner").value));
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.addEventListener("click", () => switchTab(b.dataset.tab))
  );
  $("#scenario-select").addEventListener("change", () => openScenario(+$("#scenario-select").value));
  $("#detail-close").addEventListener("click", () => $("#detail-panel").classList.add("hidden"));
  refreshScenarioSelect();
  const first = state.bootstrap.scenarios.find((s) => s.status === "draft") || state.bootstrap.scenarios[0];
  await openScenario(first.id);
}

function refreshScenarioSelect() {
  const sel = $("#scenario-select");
  sel.innerHTML = "";
  for (const s of state.bootstrap.scenarios) {
    const opt = el("option", "", `#${s.id} ${s.name}（${s.status === "draft" ? "草案" : "已确认"} v${s.version}）`);
    opt.value = s.id;
    sel.appendChild(opt);
  }
}

async function refreshBootstrap() {
  state.bootstrap = await api("GET", "/api/bootstrap");
  refreshScenarioSelect();
}

async function openScenario(id) {
  state.scenario = await api("GET", `/api/scenarios/${id}`);
  $("#scenario-select").value = id;
  renderHeader();
  renderAssumptions();
  renderGantt();
  renderConflicts();
  renderExecution();
}

function renderHeader() {
  const s = state.scenario;
  const b = $("#scenario-badges");
  b.innerHTML = "";
  b.appendChild(el("span", `badge ${s.status}`, s.status === "draft" ? "草案" : "已确认"));
  b.appendChild(el("span", "badge version", `v${s.version}`));
  if (s.updated_by) b.appendChild(el("span", "badge version", `by ${s.updated_by}`));
}

function switchTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("main .tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "compare") renderCompare();
}

// ---------- 假设编辑器 ----------
function renderAssumptions() {
  const root = $("#tab-assumptions");
  root.innerHTML = "";
  const s = state.scenario;
  const a = s.assumptions;
  const editable = s.status === "draft";

  // 工具栏
  const bar = el("div", "toolbar");
  const btnSave = el("button", "primary", "保存并重算");
  btnSave.disabled = !editable;
  btnSave.onclick = onSave;
  const btnConfirm = el("button", "", "确认方案");
  btnConfirm.disabled = !editable;
  btnConfirm.onclick = onConfirm;
  const btnRestore = el("button", "", "恢复为新草案");
  btnRestore.onclick = () => onRestore(s.id);
  const btnNew = el("button", "", "另存为新草案");
  btnNew.onclick = onClone;
  const btnRecompute = el("button", "", "重新计算");
  btnRecompute.disabled = !editable;
  btnRecompute.onclick = onRecompute;
  bar.append(btnSave, btnConfirm, btnRestore, btnNew, btnRecompute);
  if (!editable) bar.appendChild(el("span", "muted", "已确认方案为只读快照（确认时点的事前评估）；要调整请「恢复为新草案」。"));
  root.appendChild(bar);

  // 警告与冻结冲突
  if (s.schedule) {
    if (s.schedule.frozen_violations.length) {
      const d = el("div", "errlist");
      d.appendChild(el("b", "", "冻结冲突（确认将被拒绝）："));
      const ul = el("ul");
      s.schedule.frozen_violations.forEach((v) => ul.appendChild(el("li", "", v)));
      d.appendChild(ul);
      root.appendChild(d);
    }
    if (s.schedule.warnings.length) {
      const d = el("div", "warnlist");
      d.appendChild(el("b", "", "计算警告："));
      const ul = el("ul");
      s.schedule.warnings.forEach((v) => ul.appendChild(el("li", "", v)));
      d.appendChild(ul);
      root.appendChild(d);
    }
  }

  // 全局参数
  const p1 = el("div", "panel");
  p1.appendChild(el("h2", "", "全局假设"));
  const grid = el("div");
  grid.style.display = "flex";
  grid.style.gap = "16px";
  grid.style.flexWrap = "wrap";
  grid.append(
    labeledInput("方案名", "text", s.name, (v) => (s.name = v), editable),
    labeledSelect("排程策略", [
      ["critical_path", "关键路径优先（保工期）"],
      ["expected_benefit", "预期收益优先（保价值）"],
    ], a.strategy, (v) => (a.strategy = v), editable),
    labeledInput("规划 horizon（周）", "number", a.horizon_weeks, (v) => (a.horizon_weeks = +v), editable),
    labeledInput("跨项目切换成本（周）", "number", a.switch_cost_weeks, (v) => (a.switch_cost_weeks = +v), editable, 0.1)
  );
  p1.appendChild(grid);
  p1.appendChild(el("div", "muted small",
    "两种策略使用同一套资源约束：关键路径优先按「目标完成周 − 关键链长度」排序（余量小者优先）；预期收益优先按「（本项目+被卡住的下游项目）期望年化收益 ÷ 关键链长度」排序。"));
  root.appendChild(p1);

  // 人员表
  const p2 = el("div", "panel");
  p2.appendChild(el("h2", "", "人员与技能（技能格式：技能:等级, 技能:等级）"));
  const pt = el("table", "grid");
  pt.innerHTML = "<tr><th>姓名</th><th>成本(万/周)</th><th>产能(FTE)</th><th>技能</th><th></th></tr>";
  a.people.forEach((p, i) => {
    const tr = el("tr");
    tr.append(
      tdInput(p.name, (v) => (p.name = v), editable),
      tdInput(p.cost_per_week, (v) => (p.cost_per_week = +v), editable, "number"),
      tdInput(p.capacity, (v) => (p.capacity = +v), editable, "number"),
      tdInput(skillsToText(p.skills), (v) => (p.skills = parseSkills(v)), editable),
    );
    const td = el("td");
    if (editable) {
      const del = el("button", "danger small", "删");
      del.onclick = () => { a.people.splice(i, 1); renderAssumptions(); };
      td.appendChild(del);
    }
    tr.appendChild(td);
    pt.appendChild(tr);
  });
  p2.appendChild(pt);
  if (editable) {
    const add = el("button", "", "+ 加人");
    add.onclick = () => {
      a.people.push({ id: "u" + Math.random().toString(36).slice(2, 7), name: "新人", cost_per_week: 1.0, capacity: 1.0, skills: { "后端": 1 } });
      renderAssumptions();
    };
    p2.appendChild(add);
  }
  root.appendChild(p2);

  // 项目卡片
  for (const proj of a.projects) {
    root.appendChild(renderProjectEditor(proj, a, editable));
  }
  if (editable) {
    const add = el("button", "", "+ 加项目");
    add.onclick = () => {
      a.projects.push({
        id: "p" + Math.random().toString(36).slice(2, 7),
        name: "新项目", target_week: 48,
        benefit_scenarios: [{ prob: 1.0, annual_benefit: 50 }],
        phases: [{ id: "ph1", name: "阶段1", skill: state.bootstrap.skills[0], min_level: 1, effort_weeks: 2, deps: [] }],
      });
      renderAssumptions();
    };
    root.appendChild(add);
  }
}

function renderProjectEditor(proj, a, editable) {
  const card = el("div", "panel");
  const head = el("div", "toolbar");
  head.appendChild(el("h2", "", `项目：${proj.name}（${proj.id}）`));
  head.appendChild(labeledInput("名称", "text", proj.name, (v) => (proj.name = v), editable));
  head.appendChild(labeledInput("目标完成周", "number", proj.target_week, (v) => (proj.target_week = +v), editable));
  if (editable) {
    const del = el("button", "danger", "删除项目");
    del.onclick = () => { a.projects.splice(a.projects.indexOf(proj), 1); renderAssumptions(); };
    head.appendChild(del);
  }
  card.appendChild(head);

  // 收益情景
  card.appendChild(el("h3", "", "收益情景（概率 × 年化收益，万/年）—— 评估只使用这些事前假设"));
  const bt = el("table", "grid");
  bt.innerHTML = "<tr><th>概率</th><th>年化收益</th><th></th></tr>";
  proj.benefit_scenarios.forEach((sc, i) => {
    const tr = el("tr");
    tr.append(tdInput(sc.prob, (v) => (sc.prob = +v), editable, "number"),
             tdInput(sc.annual_benefit, (v) => (sc.annual_benefit = +v), editable, "number"));
    const td = el("td");
    if (editable) {
      const del = el("button", "danger small", "删");
      del.onclick = () => { proj.benefit_scenarios.splice(i, 1); renderAssumptions(); };
      td.appendChild(del);
    }
    tr.appendChild(td);
    bt.appendChild(tr);
  });
  card.appendChild(bt);
  if (editable) {
    const add = el("button", "", "+ 情景");
    add.onclick = () => { proj.benefit_scenarios.push({ prob: 0.1, annual_benefit: 100 }); renderAssumptions(); };
    card.appendChild(add);
  }
  const expB = proj.benefit_scenarios.reduce((s, x) => s + x.prob * x.annual_benefit, 0);
  card.appendChild(el("div", "muted small", `期望年化收益：${fmtNum(expB)} 万/年`));

  // 阶段表
  card.appendChild(el("h3", "", "阶段（依赖：项目内写阶段id，跨项目写 项目id/阶段id）"));
  const ht = el("table", "grid");
  ht.innerHTML = "<tr><th>id</th><th>名称</th><th>技能</th><th>最低等级</th><th>工作量(人周)</th><th>依赖</th><th></th></tr>";
  proj.phases.forEach((ph, i) => {
    const tr = el("tr");
    tr.append(tdInput(ph.id, (v) => (ph.id = v), editable), tdInput(ph.name, (v) => (ph.name = v), editable));
    // 技能下拉
    const tdSkill = el("td");
    const sel = el("select");
    state.bootstrap.skills.forEach((sk) => {
      const o = el("option", "", sk); o.value = sk; sel.appendChild(o);
    });
    sel.value = ph.skill;
    sel.disabled = !editable;
    sel.onchange = () => (ph.skill = sel.value);
    tdSkill.appendChild(sel);
    tr.append(tdSkill, tdInput(ph.min_level, (v) => (ph.min_level = +v), editable, "number"),
              tdInput(ph.effort_weeks, (v) => (ph.effort_weeks = +v), editable, "number"),
              tdInput((ph.deps || []).join(", "), (v) => (ph.deps = v.split(/[,，\s]+/).filter(Boolean)), editable));
    const td = el("td");
    if (editable) {
      const del = el("button", "danger small", "删");
      del.onclick = () => { proj.phases.splice(i, 1); renderAssumptions(); };
      td.appendChild(del);
    }
    tr.appendChild(td);
    ht.appendChild(tr);
  });
  card.appendChild(ht);
  if (editable) {
    const add = el("button", "", "+ 阶段");
    add.onclick = () => {
      proj.phases.push({ id: "ph" + (proj.phases.length + 1), name: "新阶段", skill: state.bootstrap.skills[0], min_level: 1, effort_weeks: 2, deps: [] });
      renderAssumptions();
    };
    card.appendChild(add);
  }
  return card;
}

function labeledInput(label, type, value, onChange, editable, step) {
  const lab = el("label");
  lab.appendChild(el("span", "muted", label + " "));
  const inp = el("input");
  inp.type = type;
  inp.value = value;
  if (step) inp.step = step;
  inp.disabled = !editable;
  inp.onchange = () => onChange(inp.value);
  lab.appendChild(inp);
  return lab;
}
function labeledSelect(label, options, value, onChange, editable) {
  const lab = el("label");
  lab.appendChild(el("span", "muted", label + " "));
  const sel = el("select");
  options.forEach(([v, t]) => { const o = el("option", "", t); o.value = v; sel.appendChild(o); });
  sel.value = value;
  sel.disabled = !editable;
  sel.onchange = () => onChange(sel.value);
  lab.appendChild(sel);
  return lab;
}
function tdInput(value, onChange, editable, type = "text") {
  const td = el("td");
  const inp = el("input");
  inp.type = type === "number" ? "number" : "text";
  if (type === "number") { inp.step = "any"; td.className = "num"; }
  inp.value = value;
  inp.disabled = !editable;
  inp.onchange = () => onChange(inp.value);
  td.appendChild(inp);
  return td;
}
function skillsToText(skills) {
  return Object.entries(skills).map(([k, v]) => `${k}:${v}`).join(", ");
}
function parseSkills(text) {
  const out = {};
  text.split(/[,，]/).forEach((part) => {
    const [k, v] = part.split(/[:：]/).map((x) => x && x.trim());
    if (k && !isNaN(+v)) out[k] = +v;
  });
  return out;
}

// ---------- 操作 ----------
async function onSave() {
  hideMessage();
  const s = state.scenario;
  try {
    state.scenario = await api("PUT", `/api/scenarios/${s.id}`, {
      version: s.version, assumptions: s.assumptions, by: planner(),
    });
    await refreshBootstrap();
    $("#scenario-select").value = s.id;
    renderHeader(); renderAssumptions(); renderGantt(); renderConflicts(); renderExecution();
    showMessage(`已保存并重算（v${state.scenario.version}）`, "info");
  } catch (e) {
    if (e.status === 409) {
      showMessage(`保存被拒绝：${e.message}\n别人已先保存。请刷新方案后重新修改。`, "error");
      await refreshBootstrap();
      await openScenario(s.id);
    } else showMessage(e.message, "error");
  }
}

async function onConfirm() {
  hideMessage();
  const s = state.scenario;
  if (!confirm(`确认方案「${s.name}」v${s.version}？确认后成为只读快照。`)) return;
  try {
    state.scenario = await api("POST", `/api/scenarios/${s.id}/confirm`, { version: s.version, by: planner() });
    await refreshBootstrap();
    $("#scenario-select").value = s.id;
    renderHeader(); renderAssumptions(); renderGantt(); renderConflicts(); renderExecution();
    showMessage(`方案已确认（v${state.scenario.version}），快照已固化。`, "info");
  } catch (e) {
    if (e.status === 409) {
      showMessage(`确认被拒绝：${e.message}`, "error");
      await refreshBootstrap(); await openScenario(s.id);
    } else if (e.status === 422) {
      showMessage(`确认被拒绝：${e.message}`, "error");
      renderAssumptions();
    } else showMessage(e.message, "error");
  }
}

async function onRestore(id) {
  const row = await api("POST", `/api/scenarios/${id}/restore`, { by: planner() });
  await refreshBootstrap();
  await openScenario(row.id);
  switchTab("assumptions");
  showMessage(`已恢复为新草案 #${row.id}，可修改假设并重算。`, "info");
}

async function onClone() {
  const s = state.scenario;
  const row = await api("POST", "/api/scenarios", { name: s.name + "（副本）", from_id: s.id, by: planner() });
  await refreshBootstrap();
  await openScenario(row.id);
  showMessage(`已创建新草案 #${row.id}。`, "info");
}

async function onRecompute() {
  const s = state.scenario;
  try {
    state.scenario = await api("POST", `/api/scenarios/${s.id}/compute`, {});
    renderAssumptions(); renderGantt(); renderConflicts();
    showMessage("已按最新执行记录重新计算。", "info");
  } catch (e) { showMessage(e.message, "error"); }
}

// ---------- 甘特图 ----------
function renderGantt() {
  const root = $("#tab-gantt");
  root.innerHTML = "";
  const s = state.scenario;
  if (!s.schedule) { root.appendChild(el("div", "panel", "尚未计算。")); return; }
  const sched = s.schedule;

  const bar = el("div", "toolbar");
  const bRes = el("button", state.ganttView === "resource" ? "primary" : "", "按人员（资源视角）");
  const bProj = el("button", state.ganttView === "project" ? "primary" : "", "按项目（阶段视角）");
  bRes.onclick = () => { state.ganttView = "resource"; renderGantt(); };
  bProj.onclick = () => { state.ganttView = "project"; renderGantt(); };
  bar.append(bRes, bProj);
  root.appendChild(bar);

  // 图例
  const legend = el("div", "legend");
  const pids = [...new Set(sched.assignments.map((a) => a.project_id))];
  pids.forEach((pid) => {
    const item = el("span");
    const chip = el("span", "chip"); chip.style.background = projectColor(pid);
    item.append(chip, document.createTextNode(sched.assignments.find((a) => a.project_id === pid).project_name));
    legend.appendChild(item);
  });
  const sw = el("span");
  const swc = el("span", "chip"); swc.style.background = "repeating-linear-gradient(45deg,#cbd5e1,#cbd5e1 4px,#e2e8f0 4px,#e2e8f0 8px)";
  sw.append(swc, document.createTextNode("切换成本"));
  const fz = el("span");
  const fzc = el("span", "chip"); fzc.style.background = "#94a3b8"; fzc.style.outline = "2px solid #475569";
  fz.append(fzc, document.createTextNode("已冻结（执行中/已完成）"));
  legend.append(sw, fz, el("span", "", "｜点击任意色块可追踪：谁在占用、为什么此时开始"));
  root.appendChild(legend);

  const W = sched.horizon_end; // 时间轴 0..horizon_end
  const wrap = el("div", "gantt-wrap");
  const g = el("div", "gantt");
  g.style.width = Math.max(900, 150 + W * 22) + "px";

  // 轴
  const axis = el("div", "gantt-axis");
  axis.appendChild(el("div", "label", state.ganttView === "resource" ? "人员" : "项目 / 阶段"));
  const ticks = el("div", "ticks");
  for (let w = 0; w < W; w++) ticks.appendChild(el("span", "", w % 4 === 0 ? "W" + w : ""));
  axis.appendChild(ticks);
  g.appendChild(axis);

  const addNowLine = (track) => {
    const line = el("div", "now-line");
    line.style.left = (sched.now / W * 100) + "%";
    const tag = el("div", "now-tag", "现在 W" + sched.now);
    tag.style.left = (sched.now / W * 100) + "%";
    track.append(line, tag);
  };

  if (state.ganttView === "resource") {
    for (const p of s.assumptions.people) {
      const row = el("div", "gantt-row");
      const stats = sched.people[p.id];
      const lab = el("div", "label");
      lab.append(p.name, " ");
      const sub = el("span", "sub", `利用率 ${(stats.utilization * 100).toFixed(0)}%`);
      lab.appendChild(sub);
      row.appendChild(lab);
      const track = el("div", "gantt-track");
      track.appendChild(gridBg(W, sched.now));
      sched.assignments.filter((a) => a.person_id === p.id).forEach((a) => track.appendChild(segDiv(a, W)));
      addNowLine(track);
      row.appendChild(track);
      g.appendChild(row);
    }
  } else {
    for (const proj of s.assumptions.projects) {
      const grow = el("div", "gantt-row group");
      const pm = sched.projects[proj.id];
      grow.appendChild(el("div", "label", `${proj.name}　完成 ${pm.finish ? fmtW(pm.finish) : "—"}${pm.late ? `（超期 ${fmtNum(pm.late)}w）` : ""}`));
      const gtrack = el("div", "gantt-track");
      gtrack.appendChild(gridBg(W, sched.now));
      addNowLine(gtrack);
      grow.appendChild(gtrack);
      g.appendChild(grow);
      for (const ph of proj.phases) {
        const row = el("div", "gantt-row");
        row.appendChild(el("div", "label", "　" + ph.name));
        const track = el("div", "gantt-track");
        track.appendChild(gridBg(W, sched.now));
        sched.assignments.filter((a) => a.project_id === proj.id && a.phase_id === ph.id)
          .forEach((a) => track.appendChild(segDiv(a, W)));
        addNowLine(track);
        row.appendChild(track);
        g.appendChild(row);
      }
    }
  }
  wrap.appendChild(g);
  root.appendChild(wrap);

  // 组合指标
  const pf = sched.portfolio;
  const panel = el("div", "panel");
  panel.appendChild(el("h2", "", "组合事前评估（基于当前假设，非事后实际值）"));
  const t = el("table", "grid");
  t.innerHTML = `<tr><th>期望收益(万)</th><th>人力成本(万)</th><th>期望净收益(万)</th>
    <th>P10</th><th>P50</th><th>P90</th><th>亏损概率</th><th>切换成本(周)</th></tr>
    <tr><td>${fmtNum(pf.expected_benefit)}</td><td>${fmtNum(pf.labor_cost)}</td><td><b>${fmtNum(pf.net_expected)}</b></td>
    <td>${fmtNum(pf.p10)}</td><td>${fmtNum(pf.p50)}</td><td>${fmtNum(pf.p90)}</td>
    <td>${(pf.prob_negative * 100).toFixed(1)}%</td><td>${fmtNum(pf.switch_weeks_total)}</td></tr>`;
  panel.appendChild(t);
  panel.appendChild(el("div", "muted small",
    "P10/P50/P90 来自对收益情景的蒙特卡洛抽样（2000 次，固定种子）。同一个决策在不同情景下结果不同——评价决策应看它当时面对的信息，而不是事后落到哪个情景。"));
  root.appendChild(panel);
}

function gridBg(W, now) {
  const bg = el("div", "gantt-grid");
  for (let w = 0; w < W; w++) {
    const cell = el("div", "wk" + (w === Math.floor(now) ? " now" : ""));
    bg.appendChild(cell);
  }
  return bg;
}

function segDiv(a, W) {
  const d = el("div", "seg" + (a.kind === "switch" ? " switch" : "") + (a.frozen ? " frozen" : ""));
  d.style.left = (a.start / W * 100) + "%";
  d.style.width = Math.max(0.4, (a.end - a.start) / W * 100) + "%";
  if (a.kind === "work") d.style.background = projectColor(a.project_id);
  d.textContent = a.kind === "switch" ? "切换" : a.phase_name;
  d.title = `${a.project_name}/${a.phase_name} ${fmtW(a.start)}–${fmtW(a.end)}`;
  d.onclick = () => showSegmentDetail(a);
  return d;
}

function showSegmentDetail(a) {
  const panel = $("#detail-panel");
  const c = $("#detail-content");
  c.innerHTML = "";
  c.appendChild(el("h3", "", a.kind === "switch" ? "切换成本" : "阶段占用"));
  const dl = el("dl");
  const rows = [
    ["项目", a.project_name], ["阶段", a.phase_name], ["人员", a.person_name],
    ["区间", `${fmtW(a.start)} – ${fmtW(a.end)}（${fmtNum(a.end - a.start)} 周）`],
    ["类型", a.frozen ? "已冻结（执行记录）" : a.kind === "switch" ? "项目切换" : "计划工作"],
  ];
  if (a.skill) rows.push(["技能", a.skill]);
  rows.forEach(([k, v]) => { dl.append(el("dt", "", k), el("dd", "", v)); });
  c.appendChild(dl);
  c.appendChild(el("h3", "", "为什么排在这里"));
  const ul = el("ul");
  (a.reasons || []).forEach((r) => ul.appendChild(el("li", "", r)));
  c.appendChild(ul);
  panel.classList.remove("hidden");
}

// ---------- 冲突与瓶颈 ----------
function renderConflicts() {
  const root = $("#tab-conflicts");
  root.innerHTML = "";
  const s = state.scenario;
  if (!s.schedule) return;
  const sched = s.schedule;

  // 资源等待列表
  const p1 = el("div", "panel");
  p1.appendChild(el("h2", "", "资源等待（谁被谁卡住）"));
  if (!sched.waits.length) {
    p1.appendChild(el("div", "muted", "没有因资源占用产生的等待。"));
  } else {
    const t = el("table", "grid");
    t.innerHTML = "<tr><th>被推迟的阶段</th><th>等待(周)</th><th>占用者</th><th>占用区间</th></tr>";
    sched.waits.forEach((w) => {
      const tr = el("tr");
      tr.appendChild(el("td", "", `${w.project_name}/${w.phase_name}`));
      tr.appendChild(el("td", "", fmtNum(w.weeks)));
      tr.appendChild(el("td", "", `${w.person_name} ← 「${w.blocker.project_name}/${w.blocker.phase_name}」`));
      tr.appendChild(el("td", "", `${fmtW(w.blocker.start)}–${fmtW(w.blocker.end)}`));
      t.appendChild(tr);
    });
    p1.appendChild(t);
  }
  root.appendChild(p1);

  // 技能瓶颈
  const p2 = el("div", "panel");
  p2.appendChild(el("h2", "", "技能瓶颈（按等级带）"));
  const t2 = el("table", "grid");
  t2.innerHTML = "<tr><th>技能</th><th>等级带</th><th>需求(人周)</th><th>合格人员</th><th>负荷率</th></tr>";
  Object.entries(sched.skills).forEach(([skill, st]) => {
    st.bands.forEach((band, bi) => {
      const tr = el("tr");
      if (bi === 0) {
        const td = el("td", "", skill);
        td.rowSpan = st.bands.length;
        tr.appendChild(td);
      }
      tr.appendChild(el("td", "", `≥${band.min_level}`));
      tr.appendChild(el("td", "", fmtNum(band.demand_weeks)));
      tr.appendChild(el("td", "", band.qualified.join("、")));
      const td = el("td");
      const ratio = band.load_ratio || 0;
      const meter = el("div", "meter" + (ratio > 0.5 ? " hot" : ""));
      const fill = el("i");
      fill.style.width = Math.min(100, ratio * 100) + "%";
      meter.appendChild(fill);
      td.append(meter, el("span", "meter-label", ` ${(ratio * 100).toFixed(0)}%`));
      tr.appendChild(td);
      t2.appendChild(tr);
    });
  });
  p2.appendChild(t2);
  p2.appendChild(el("div", "muted small",
    "负荷率 = 该等级带需求人周 ÷ 合格人员在 horizon 内的总产能。瓶颈通常只在最高等级带出现（如「数据≥3 只有赵敏」），>50% 标红。"));
  root.appendChild(p2);

  // 人员利用率与切换
  const p3 = el("div", "panel");
  p3.appendChild(el("h2", "", "人员利用率与切换成本"));
  const t3 = el("table", "grid");
  t3.innerHTML = "<tr><th>人员</th><th>利用率</th><th>切换成本(周)</th></tr>";
  Object.values(sched.people).forEach((p) => {
    const tr = el("tr");
    tr.appendChild(el("td", "", p.name));
    const td = el("td");
    const meter = el("div", "meter" + (p.utilization > 0.9 ? " hot" : ""));
    const fill = el("i");
    fill.style.width = Math.min(100, p.utilization * 100) + "%";
    meter.appendChild(fill);
    td.append(meter, el("span", "meter-label", ` ${(p.utilization * 100).toFixed(0)}%（${fmtNum(p.busy_weeks)}周）`));
    tr.appendChild(td);
    tr.appendChild(el("td", "", fmtNum(p.switch_weeks)));
    t3.appendChild(tr);
  });
  p3.appendChild(t3);
  root.appendChild(p3);
}

// ---------- 方案比较 ----------
async function renderCompare() {
  const root = $("#tab-compare");
  root.innerHTML = "";
  const panel = el("div", "panel");
  panel.appendChild(el("h2", "", "选择要比较的方案（2–4 个）"));
  const list = el("div", "toolbar");
  state.bootstrap.scenarios.forEach((s) => {
    const lab = el("label");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = state.compareIds.has(s.id);
    cb.onchange = () => { cb.checked ? state.compareIds.add(s.id) : state.compareIds.delete(s.id); };
    lab.append(cb, document.createTextNode(` #${s.id} ${s.name}（${s.status === "draft" ? "草案" : "已确认"}）`));
    list.appendChild(lab);
  });
  const go = el("button", "primary", "比较");
  go.onclick = () => renderCompareResult(root);
  panel.append(list, go);
  panel.appendChild(el("div", "note",
    "比较口径：全部使用各方案保存/确认时的事前假设与快照（期望收益、蒙特卡洛分位），不使用任何事后实际值。" +
    "已确认方案的快照固定在确认时点；草案反映其最近一次计算。"));
  root.appendChild(panel);
  const holder = el("div");
  holder.id = "compare-result";
  root.appendChild(holder);
  if (state.compareIds.size >= 2) renderCompareResult(root);
}

async function renderCompareResult(root) {
  const holder = $("#compare-result");
  holder.innerHTML = "";
  const ids = [...state.compareIds].slice(0, 4);
  if (ids.length < 2) return;
  const rows = await api("GET", "/api/compare?ids=" + ids.join(","));
  if (rows.length < 2) { holder.appendChild(el("div", "panel", "可比较的方案不足（草案需先计算）。")); return; }

  // 指标对比表
  const panel = el("div", "panel");
  panel.appendChild(el("h2", "", "组合指标对比"));
  const t = el("table", "grid cmp-table");
  const metrics = [
    ["策略", (r) => r.strategy === "critical_path" ? "关键路径优先" : "预期收益优先", null],
    ["期望净收益(万)", (r) => fmtNum(r.portfolio.net_expected), (r) => r.portfolio.net_expected],
    ["P10（悲观）", (r) => fmtNum(r.portfolio.p10), (r) => r.portfolio.p10],
    ["P50（中位）", (r) => fmtNum(r.portfolio.p50), (r) => r.portfolio.p50],
    ["P90（乐观）", (r) => fmtNum(r.portfolio.p90), (r) => r.portfolio.p90],
    ["亏损概率", (r) => (r.portfolio.prob_negative * 100).toFixed(1) + "%", (r) => -r.portfolio.prob_negative],
    ["人力成本(万)", (r) => fmtNum(r.portfolio.labor_cost), (r) => -r.portfolio.labor_cost],
    ["切换成本(周)", (r) => fmtNum(r.portfolio.switch_weeks_total), (r) => -r.portfolio.switch_weeks_total],
  ];
  const head = el("tr");
  head.appendChild(el("th", "", "指标"));
  rows.forEach((r) => head.appendChild(el("th", "scen", `#${r.id} ${r.name}`)));
  t.appendChild(head);
  metrics.forEach(([label, fmt, val]) => {
    const tr = el("tr");
    tr.appendChild(el("td", "", label));
    let best = -1;
    if (val) {
      let bestV = -Infinity;
      rows.forEach((r, i) => { const v = val(r); if (v > bestV) { bestV = v; best = i; } });
    }
    rows.forEach((r, i) => {
      const td = el("td", i === best ? "cmp-best" : "", fmt(r));
      tr.appendChild(td);
    });
    t.appendChild(tr);
  });
  // 每个项目完成周
  const pids = [...new Set(rows.flatMap((r) => Object.keys(r.projects)))];
  pids.forEach((pid) => {
    const tr = el("tr");
    const name = rows[0].projects[pid] ? rows[0].projects[pid].name : pid;
    tr.appendChild(el("td", "muted", `完成周：${name}`));
    const fins = rows.map((r) => (r.projects[pid] ? r.projects[pid].finish : null));
    const valid = fins.filter((f) => f !== null);
    const minF = Math.min(...valid);
    fins.forEach((f) => tr.appendChild(el("td", f === minF ? "cmp-best" : "", f ? fmtW(f) : "—")));
    t.appendChild(tr);
  });
  panel.appendChild(t);
  panel.appendChild(el("div", "muted small", "绿色为该指标下最优。注意：期望净收益最高的方案通常完成周更晚或切换更多——这正是「保关键路径」与「保预期收益」的取舍，没有免费的最优。"));
  holder.appendChild(panel);

  // 完成周条形图
  const p2 = el("div", "panel");
  p2.appendChild(el("h2", "", "各项目完成周对比"));
  const maxW = Math.max(...rows.map((r) => r.horizon_end));
  const bars = el("div", "finish-bars");
  pids.forEach((pid) => {
    const name = rows[0].projects[pid] ? rows[0].projects[pid].name : pid;
    rows.forEach((r, i) => {
      const m = r.projects[pid];
      if (!m || m.finish === null) return;
      const row = el("div", "row");
      row.appendChild(el("div", "name", `${name} · #${r.id}`));
      const bar = el("div", "bar");
      bar.style.background = PALETTE[i % PALETTE.length];
      bar.style.width = (m.finish / maxW * 60) + "%";
      bar.appendChild(el("span", "", fmtW(m.finish)));
      row.appendChild(bar);
      bars.appendChild(row);
    });
  });
  p2.appendChild(bars);
  holder.appendChild(p2);

  // 恢复按钮
  const p3 = el("div", "panel");
  p3.appendChild(el("h2", "", "基于比较结果行动"));
  const bar = el("div", "toolbar");
  rows.forEach((r) => {
    const b = el("button", "", `恢复 #${r.id} 为新草案`);
    b.onclick = () => onRestore(r.id);
    bar.appendChild(b);
  });
  p3.append(bar, el("div", "muted small", "恢复会复制该方案的全部假设生成新草案，可修改后重新计算、再确认。"));
  holder.appendChild(p3);
}

// ---------- 执行与确认 ----------
function renderExecution() {
  const root = $("#tab-execution");
  root.innerHTML = "";
  const now = state.bootstrap.now;
  const exec = state.bootstrap.execution;

  const p1 = el("div", "panel");
  p1.appendChild(el("h2", "", `执行记录（当前周 W${now}）`));
  const t = el("table", "grid");
  t.innerHTML = "<tr><th>阶段</th><th>执行人</th><th>区间</th><th>状态</th><th>备注</th></tr>";
  const peopleById = Object.fromEntries(state.scenario.assumptions.people.map((p) => [p.id, p]));
  exec.forEach((r) => {
    const tr = el("tr");
    tr.appendChild(el("td", "", r.phase_key));
    tr.appendChild(el("td", "", peopleById[r.person_id] ? peopleById[r.person_id].name : r.person_id));
    tr.appendChild(el("td", "", `${fmtW(r.start_week)}–${fmtW(r.end_week)}`));
    tr.appendChild(el("td", "", r.status === "done" ? "已完成" : "进行中"));
    tr.appendChild(el("td", "", r.note || ""));
    t.appendChild(tr);
  });
  p1.appendChild(t);
  p1.appendChild(el("div", "muted small",
    "这些阶段已冻结：任何方案确认时都会校验它们的位置、工期、执行人未被修改；已开始阶段不会被新方案悄悄移动。"));
  root.appendChild(p1);

  // 标记开始（演示用）
  const p2 = el("div", "panel");
  p2.appendChild(el("h2", "", "标记阶段开始（写入执行记录）"));
  const frozenKeys = new Set(exec.map((r) => r.phase_key));
  const form = el("div", "toolbar");
  const phSel = el("select");
  state.scenario.assumptions.projects.forEach((proj) =>
    proj.phases.forEach((ph) => {
      const key = `${proj.id}/${ph.id}`;
      if (frozenKeys.has(key)) return;
      const o = el("option", "", `${key}（${ph.name}，${ph.skill}≥${ph.min_level}）`);
      o.value = JSON.stringify({ pid: proj.id, phid: ph.id });
      phSel.appendChild(o);
    })
  );
  const pSel = el("select");
  state.scenario.assumptions.people.forEach((p) => {
    const o = el("option", "", `${p.name}（${skillsToText(p.skills)}）`);
    o.value = p.id;
    pSel.appendChild(o);
  });
  const btn = el("button", "", "从当前周开始");
  btn.onclick = async () => {
    if (!phSel.value) return;
    const { pid, phid } = JSON.parse(phSel.value);
    try {
      const res = await api("POST", "/api/execution/start", {
        scenario_id: state.scenario.id, project_id: pid, phase_id: phid,
        person_id: pSel.value, by: planner(),
      });
      state.bootstrap.execution = res.records;
      renderExecution();
      showMessage(`已标记 ${pid}/${phid} 从 W${res.now} 开始。注意：草案需「重新计算」才会反映该冻结记录；已确认方案的快照保持不变（那是确认时点的事前评估）。`, "info");
    } catch (e) { showMessage(e.message, "error"); }
  };
  form.append(el("span", "muted", "阶段"), phSel, el("span", "muted", "执行人"), pSel, btn);
  p2.appendChild(form);
  p2.appendChild(el("div", "muted small",
    "试试：标记一个阶段开始 → 在草案里修改该阶段的工期或技能要求 → 「确认方案」会被拒绝并列出冻结冲突。"));
  root.appendChild(p2);
}

init().catch((e) => showMessage("初始化失败: " + e.message, "error"));
