"use strict";
// The Costs view: /api/costs rendered as figures, a daily stacked chart, the allocation table, budgets, nodes and
// findings. Read-only: it never POSTs. Every name comes from the cluster, so it goes through esc() or textContent.

const costs = {data: null, by: "namespace", days: 30, timer: null, loading: false, error: null};
const COSTS_REFRESH_MS = 60000;
const usd0 = (v) => "$" + Math.round(v).toLocaleString();
const usd2 = (v) => "$" + v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const pct0 = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
// Server text with `commands` in backticks: escaped first, then the backticked parts become <code>.
const withCode = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
const FINDING = {
  "idle-workload": "Abandoned workload", "over-requested": "Over-requested", "unused-volume": "Unused volume",
  "no-requests": "No requests set", "idle-capacity": "Idle capacity", "cost-spike": "Cost spike",
};
const BUDGET = {over: {label: "Over budget", color: "var(--crit)"}, "at-risk": {label: "At risk", color: "var(--warn)"},
  ok: {label: "Within budget", color: "var(--good)"}, unknown: {label: "Nothing ran", color: "var(--muted)"}};

async function loadCosts() {
  if (costs.loading) return;
  costs.loading = true;
  $("#costs-refresh").disabled = true;
  try {
    costs.data = await api(`/api/costs?days=${costs.days}`);
    costs.error = null;
  } catch (e) {
    costs.error = e.message;
  }
  costs.loading = false;
  $("#costs-refresh").disabled = false;
  renderCosts();
}

function startCosts() {
  if (!costs.data) loadCosts(); else renderCosts();  // the chart is laid out for the width it has now
  clearInterval(costs.timer);
  costs.timer = setInterval(loadCosts, COSTS_REFRESH_MS);
}
function stopCosts() { clearInterval(costs.timer); costs.timer = null; }

function initCosts() {
  $("#costs-refresh").onclick = loadCosts;
  $("#costs-days").onchange = (ev) => { costs.days = Number(ev.target.value); costs.data = null; renderCosts(); loadCosts(); };
  $$("#costs-by .chip").forEach((b) => (b.onclick = () => {
    costs.by = b.dataset.by;
    $$("#costs-by .chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    renderCostsTable();
  }));
  $("#costs-export").onclick = () => costs.data && download(`kubesage-costs-by-${costs.by}.csv`, costsCsv(), "text/csv");
  bindTips($("#costs-trend"), describeCostBar, () => {});
  new ResizeObserver(() => { if (costs.data && !$("#costs").hidden) renderCostsTrend(); }).observe($("#costs-trend"));
}

function costRows() {
  const r = costs.data.report;
  return {namespace: r.by_namespace, workload: r.by_workload, team: r.by_team}[costs.by];
}

function renderCosts() {
  const note = $("#costs-notice");
  note.hidden = !costs.error;
  if (costs.error) {
    note.dataset.kind = "err";
    $("#costs-notice-text").textContent = `Could not read the cluster's costs: ${costs.error}`;
  }
  $("#costs-export").disabled = !costs.data;
  if (!costs.data) {
    $("#costs-info").textContent = costs.error ? "No cost report" : "Reading the cluster's bill…";
    $("#costs-figs").innerHTML = fig("Cluster cost", "—", "per month", "hero-fig") + fig("Idle", "—", "paid for, not requested")
      + fig("Efficiency", "—", "used of what is billed") + fig("Savings to check", "—", "from the findings");
    ["#costs-trend", "#costs-table", "#costs-budgets", "#costs-nodes", "#costs-findings", "#costs-notes", "#costs-legend"]
      .forEach((s) => ($(s).innerHTML = ""));
    $("#costs-table-note").textContent = "";
    $("#costs-findings-note").textContent = "";
    return;
  }
  const r = costs.data.report, c = r.cluster;
  $("#costs-info").textContent = `Last ${r.observed_days} of ${r.window_days} days · ${r.by_namespace.length} namespaces · `
    + `${r.by_workload.length} workloads · updated ${new Date(r.generated_at).toLocaleTimeString()}`;
  const over = r.budgets.filter((b) => b.status === "over").length;
  $("#costs-figs").innerHTML =
    fig("Cluster cost", `${usd0(c.monthly_usd)}<small> /mo</small>`,
      `nodes ${usd0(c.nodes_monthly_usd)} · volumes ${usd0(c.storage_monthly_usd)}`
      + (over ? ` · <span class="nw" style="color:var(--crit)">${over} budget${over > 1 ? "s" : ""} over</span>` : ""), "hero-fig")
    + fig("Idle", `${usd0(c.idle_monthly_usd)}<small> /mo · ${Math.round(c.idle_pct)}%</small>`,
      `${c.cpu_request_cores} of ${c.cpu_capacity_cores} cores requested`)
    + fig("Efficiency", pct0(c.efficiency), `${c.cpu_used_cores} cores and ${c.mem_used_gib} GiB used on average`)
    + fig("Savings to check", `${usd0(r.potential_monthly_usd)}<small> /mo</small>`,
      `${r.findings.filter((f) => f.counted).length} findings · reach the bill when nodes go`);
  renderCostsTrend();
  renderCostsTable();
  renderCostsBudgets();
  renderCostsNodes();
  renderCostsFindings();
  $("#costs-notes").innerHTML = r.notes.map((n) => `<li>${esc(n)}</li>`).join("");
}

/* Daily stacked bars: the seven largest namespaces, the rest together, and idle capacity on top in neutral grey. */
function costSeries() {
  const r = costs.data.report, top = r.by_namespace.slice(0, 7), rest = r.by_namespace.slice(7);
  const series = top.map((l, i) => ({name: l.name, color: `var(--s${i + 1})`, daily: l.daily_usd}));
  if (rest.length) {
    series.push({name: `${rest.length} more`, color: "var(--muted)",
      daily: r.days.map((_, d) => rest.reduce((a, l) => a + l.daily_usd[d], 0))});
  }
  series.push({name: "Idle capacity", color: "var(--track)", idle: true, daily: r.cluster.daily_idle_usd});
  return series;
}

function renderCostsTrend() {
  const el = $("#costs-trend"), r = costs.data.report, series = costSeries(), n = r.days.length;
  const W = Math.max(el.clientWidth, 320), H = 220, left = 46, bottom = 22, top = 8, plotW = W - left - 8, plotH = H - top - bottom;
  const totals = r.days.map((_, d) => series.reduce((a, s) => a + s.daily[d], 0));
  const max = Math.max(...totals, 0.01), step = niceStep(max / 4), yMax = Math.ceil(max / step) * step;
  const slot = plotW / n, bw = Math.max(2, Math.min(28, slot - (slot > 10 ? 3 : 1)));
  const y = (v) => top + plotH - (v / yMax) * plotH;
  let g = "";
  for (let v = 0; v <= yMax + 1e-9; v += step) {
    g += `<line class="grid" x1="${left}" x2="${W - 8}" y1="${y(v)}" y2="${y(v)}"/><text class="tick" x="${left - 6}" y="${y(v) + 4}" text-anchor="end">$${fmtAxis(v)}</text>`;
  }
  const every = Math.ceil(n / Math.max(1, Math.floor(plotW / 62)));
  r.days.forEach((day, d) => {
    const x = left + d * slot + (slot - bw) / 2;
    let acc = 0, bars = "";
    series.forEach((s) => {
      const v = s.daily[d];
      if (v <= 0) return;
      const y1 = y(acc + v), h = y(acc) - y1;
      bars += `<rect class="${s.idle ? "idle" : ""}" x="${x}" y="${y1}" width="${bw}" height="${Math.max(h, 0.5)}" fill="${s.color}"/>`;
      acc += v;
    });
    g += `<g data-tip data-day="${d}" tabindex="0" role="img" aria-label="${esc(day)}: ${usd2(totals[d])}">`
      + `<rect class="hit" x="${left + d * slot}" y="${top}" width="${slot}" height="${plotH}"/>${bars}</g>`;
    if ((n - 1 - d) % every === 0) g += `<text class="tick" x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(day.slice(5))}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="group" aria-label="Daily cluster cost by namespace">${g}</svg>`;
  $("#costs-legend").innerHTML = series.map((s) =>
    `<span class="costs-key"><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
}
const niceStep = (raw) => { const p = 10 ** Math.floor(Math.log10(raw)), m = raw / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; };
const fmtAxis = (v) => (v >= 10 || v === 0 ? Math.round(v) : v.toFixed(v >= 1 ? 1 : 2));

function describeCostBar(t) {
  const r = costs.data.report, d = Number(t.dataset.day), series = costSeries();
  const rows = series.filter((s) => s.daily[d] > 0).sort((a, b) => (a.idle ? 1 : b.idle ? -1 : b.daily[d] - a.daily[d]))
    .map((s) => [s.name, usd2(s.daily[d])]);
  return {title: r.days[d], sub: `${usd2(series.reduce((a, s) => a + s.daily[d], 0))} for the day`, rows};
}

function renderCostsTable() {
  if (!costs.data) return;
  const rows = costRows(), wl = costs.by === "workload", max = Math.max(...rows.map((l) => l.monthly_usd), 0.01);
  $("#costs-table-note").textContent = {
    namespace: "Direct cost of each namespace. “With shared” adds its part of the shared namespaces and of idle capacity, so that column adds up to the whole bill.",
    workload: "Pods rolled up to the controller that owns them. A volume nothing mounts is listed as its own row.",
    team: `From the “${costs.data.config.team_label}” label on pods and namespaces, then config/costs.yaml.`}[costs.by];
  const head = `<thead><tr><th scope="col">${wl ? "Workload" : costs.by === "team" ? "Team" : "Namespace"}</th>`
    + `<th scope="col" class="n">Pods</th><th scope="col" class="n">CPU req / used</th><th scope="col" class="n">Memory req / used</th>`
    + `<th scope="col" class="n">Efficiency</th><th scope="col" class="n">Per month</th><th scope="col" class="n">With shared</th>`
    + `<th scope="col" class="n">Share</th></tr></thead>`;
  const body = rows.map((l) => {
    const sub = wl ? `<span class="muted">${esc(l.namespace)} · ${esc(l.kind)}</span>` : l.shared ? `<span class="muted">shared</span>`
      : costs.by === "namespace" && l.team ? `<span class="muted">${esc(l.team)}</span>` : "";
    const eff = l.efficiency == null ? "—" : `<span class="eff" style="--v:${Math.round(l.efficiency * 100)}%"><i></i></span>${pct0(l.efficiency)}`;
    return `<tr><th scope="row"><span class="nm">${esc(l.name)}</span>${sub}</th><td class="n">${l.pods}</td>`
      + `<td class="n">${l.cpu_request_cores.toFixed(2)} / ${l.cpu_used_cores.toFixed(2)}</td>`
      + `<td class="n">${l.mem_request_gib.toFixed(2)} / ${l.mem_used_gib.toFixed(2)} GiB</td><td class="n">${eff}</td>`
      + `<td class="n money"><span class="mbar" style="--v:${(l.monthly_usd / max * 100).toFixed(1)}%"></span>${usd2(l.monthly_usd)}</td>`
      + `<td class="n">${l.shared ? "·" : usd2(l.monthly_loaded_usd)}</td><td class="n">${l.shared ? "·" : Math.round(l.share_pct) + "%"}</td></tr>`;
  }).join("");
  $("#costs-table").innerHTML = head + `<tbody>${body}</tbody>`;
}

function renderCostsBudgets() {
  const r = costs.data.report;
  if (!r.budgets.length) {
    $("#costs-budgets").innerHTML = `<p class="empty">No budgets yet. Add some under <code>budgets:</code> in <code>config/costs.yaml</code>.</p>`;
    return;
  }
  $("#costs-budgets").innerHTML = r.budgets.map((b) => {
    const st = BUDGET[b.status], used = b.used_pct ?? 0;
    const who = b.scope === "cluster" ? "<b>Whole cluster</b>" : `<b>${esc(b.name)}</b><span class="muted">${esc(b.scope)}</span>`;
    return `<div class="budget"><div class="budget-h">${who}`
      + `<span class="badge"><span class="pulse-dot" style="--c:${st.color}"></span>${st.label}</span></div>`
      + `<div class="budget-bar" role="img" aria-label="${Math.round(used)}% of the budget"><i style="width:${Math.min(used, 100)}%;background:${st.color}"></i></div>`
      + `<div class="budget-f">${b.monthly_usd == null ? "No cost in the window" : `${usd2(b.monthly_usd)} of ${usd2(b.budget_usd)} · ${Math.round(used)}%`}</div></div>`;
  }).join("");
}

function renderCostsNodes() {
  const r = costs.data.report, c = r.cluster;
  const how = {label: "SKU label", capacity: "matched by size", "unit-price": "priced by size", assumed: "assumed", config: "configured pool"};
  $("#costs-nodes").innerHTML = `<div class="costs-scroll"><table class="costs-table"><thead><tr><th scope="col">Node</th><th scope="col">SKU</th>`
    + `<th scope="col" class="n">Per hour</th><th scope="col" class="n">Window</th></tr></thead><tbody>`
    + r.nodes.map((n) => `<tr><th scope="row"><span class="nm">${esc(n.name)}</span></th><td>${esc(n.sku)} <span class="muted">${how[n.priced_by] || esc(n.priced_by)}</span></td>`
      + `<td class="n">$${n.hourly_usd.toFixed(3)}</td><td class="n">${usd2(n.usd)}</td></tr>`).join("")
    + `</tbody></table></div><p class="muted costs-unit">Blended price: $${c.core_hour_usd.toFixed(4)} per core-hour, $${c.gib_hour_usd.toFixed(4)} per GiB-hour.</p>`;
}

function renderCostsFindings() {
  const r = costs.data.report;
  $("#costs-findings-note").textContent = r.findings.length
    ? `${usd2(r.potential_monthly_usd)} a month in savings to check. Risks and notes are listed without a saving.`
    : "";
  if (!r.findings.length) {
    $("#costs-findings").innerHTML = `<p class="empty">Nothing stands out in this window.</p>`;
    return;
  }
  const tone = {saving: "var(--good)", risk: "var(--warn)", info: "var(--muted)"};
  $("#costs-findings").innerHTML = r.findings.map((f) =>
    `<article class="finding"><div class="finding-h"><span class="badge"><span class="pulse-dot" style="--c:${tone[f.severity]}"></span>${FINDING[f.kind] || esc(f.kind)}</span>`
    + `<b>${esc(f.target)}</b>${f.monthly_usd ? `<span class="finding-usd">${usd2(f.monthly_usd)}<small> /mo${f.counted ? "" : f.kind === "cost-spike" ? " if it stays" : " idle"}</small></span>` : ""}</div>`
    + `<p>${esc(f.title)}. ${esc(f.detail)}</p><p class="muted">${withCode(f.action)}</p></article>`).join("");
}

function costsCsv() {
  const cell = (v) => { const s = String(v ?? ""); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const cols = ["name", "namespace", "kind", "team", "shared", "pods", "cpu_request_cores", "cpu_used_cores", "mem_request_gib",
    "mem_used_gib", "efficiency", "cpu_usd", "ram_usd", "storage_usd", "total_usd", "monthly_usd", "monthly_loaded_usd", "share_pct"];
  return [cols.join(","), ...costRows().map((l) => cols.map((k) => cell(l[k])).join(","))].join("\n") + "\n";
}
