"use strict";
// FinOps dashboard: figures, the isometric cluster, charts, the router flow, recommendations and the three steps.

/* Headline reveal: a violet-cyan-pink edge sweeps across the text and settles into ink. */
function sweep(el) {
  if (reduceMotion.matches) return;
  const t0 = performance.now(), dur = 1600;
  el.classList.add("sweeping");
  const frame = (t) => {
    const k = Math.min(1, (t - t0) / dur), p = (1 - (1 - k) ** 3) * 160;
    el.style.backgroundImage = `linear-gradient(90deg, var(--fg) ${p - 60}%, #7c5cff ${p - 38}%, #1fc8ff ${p - 19}%, #ff3dc0 ${p}%, transparent ${p}%)`;
    if (k < 1) requestAnimationFrame(frame);
    else { el.classList.remove("sweeping"); el.style.backgroundImage = ""; }
  };
  requestAnimationFrame(frame);
}

/* Tooltip: values come from the cluster and the model, so it is built with textContent only. */
const tip = $("#tip");
function showTip(x, y, {title, sub, rows}) {
  tip.replaceChildren();
  const add = (cls, text) => { const d = document.createElement("div"); d.className = cls; d.textContent = text; tip.append(d); return d; };
  add("t", title);
  if (sub) add("s", sub);
  rows.forEach(([label, value]) => {
    const r = add("r", ""), a = document.createElement("span"), b = document.createElement("b");
    a.textContent = label; b.textContent = value; r.append(a, b);
  });
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.max(8, Math.min(x + 14, innerWidth - w - 8)) + "px";
  tip.style.top = (y + h + 22 > innerHeight ? y - h - 12 : y + 16) + "px";
}
const hideTip = () => (tip.hidden = true);
function bindTips(root, describe, onSelect) {
  const target = (ev) => ev.target.closest("[data-tip]");
  root.addEventListener("pointermove", (ev) => { const t = target(ev); t ? showTip(ev.clientX, ev.clientY, describe(t)) : hideTip(); });
  root.addEventListener("pointerleave", hideTip);
  root.addEventListener("focusin", (ev) => {
    const t = target(ev); if (!t) return;
    t.setAttribute("aria-describedby", "tip");
    const b = t.getBoundingClientRect();
    showTip(b.left + b.width / 2, b.bottom, describe(t));
  });
  root.addEventListener("focusout", (ev) => { target(ev)?.removeAttribute("aria-describedby"); hideTip(); });
  root.addEventListener("click", (ev) => onSelect(target(ev)));
  root.addEventListener("keydown", (ev) => {
    if ((ev.key === "Enter" || ev.key === " ") && target(ev)) { ev.preventDefault(); onSelect(target(ev)); }
  });
}

function select(next) {
  sel = next && sel && next.kind === sel.kind && next.id === sel.id ? null : next;
  applyFocus();
  renderInspector();
  hideTip();
}

function applyFocus() {
  const f = sel?.kind === "app" ? sel.id : hoverApp;
  $$("[data-focus-scope]").forEach((s) => s.classList.toggle("focus", !!f));
  $$("[data-focus-scope] [data-app]").forEach((e) => e.classList.toggle("hl", e.dataset.app === f));
  $$(".chip[data-app]").forEach((c) => c.setAttribute("aria-pressed", String(sel?.kind === "app" && sel.id === c.dataset.app)));
  $$("#scene [data-node]").forEach((g) => g.classList.toggle("sel", sel?.kind === "node" && sel.id === g.dataset.node));
}

/* ---------- Hero figures ---------- */

function hbar(x, y, w, h, r = Math.min(4, h / 2)) {  // a bar with a rounded data end, square at the baseline
  w = Math.max(w, 0.5); r = Math.min(r, w);
  return `M${x},${y}H${x + w - r}A${r},${r} 0 0 1 ${x + w},${y + r}V${y + h - r}A${r},${r} 0 0 1 ${x + w - r},${y + h}H${x}Z`;
}

function fig(label, value, sub, cls = "", extra = "") {
  return `<div class="fig ${cls}"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div>${extra}</div>`;
}

function renderFigures() {
  const r = report, el = $("#figures");
  if (!r) {
    el.innerHTML = fig("Saved per month", "—", "Press <b>Analyze</b> to find out.", "hero-fig")
      + fig("Reserved capacity", "—", "allocation view") + fig("Cost to find it", "—", "LLM spend per run") + fig("Workloads", "—", "triaged");
    return;
  }
  const np = r.node_pool, counts = {HEALTHY: 0, ROUTINE: 0, COMPLEX: 0};
  r.workloads.forEach((w) => counts[w.triage.status]++);
  const before = sum(r.workloads, "monthly_before_usd"), after = sum(r.workloads, "monthly_after_usd");
  let hero;
  if (np) {
    const max = Math.max(np.before_monthly_usd, np.after_monthly_usd) || 1, span = 220;
    const bar = (y, v, cls, label) => `<text class="tick" x="0" y="${y - 5}">${esc(label)}</text>
      <path class="${cls}" d="${hbar(0, y, (v / max) * span, 10)}"/><text class="val" x="${(v / max) * span + 6}" y="${y + 9}">${usd(v)}</text>`;
    const savings = np.before_monthly_usd - np.after_monthly_usd;
    hero = fig(savings >= 0 ? "Potential monthly savings" : "Additional monthly capacity", `${usd(Math.abs(savings))}<small> /mo</small>`,
      savings >= 0 ? "estimated node-pool cost reduction" : "more capacity to support the proposed scaling plan", "hero-fig",
      `<svg class="poolbars" viewBox="0 0 300 66" role="img" aria-label="Node pool cost before and after">
        ${bar(14, np.before_monthly_usd, "trk", `Before · ${np.before_nodes}× ${np.before_sku}`)}
        ${bar(50, np.after_monthly_usd, "usebar", `After · ${np.choice.nodes}× ${np.choice.sku}`)}</svg>`);
  } else {
    hero = fig("Saved per month · node pool", "—", "No node-pool change recommended.", "hero-fig");
  }
  el.innerHTML = hero
    + fig("Planned reservations", `${usd(after)}<small> /mo</small>`, `was ${usd(before)} · allocation estimate`)
    + fig("Cost to find it", usd(r.llm_cost_usd), `${r.ledger.length} LLM calls${r.mode === "mock" ? " · estimated (mock)" : ""}`)
    + fig("Workloads", String(r.workloads.length), Object.keys(STATUS).map((k) =>
        `<span class="nw"><span style="color:${STATUS[k].color}">${icon(STATUS[k].icon)}</span> ${counts[k]} ${STATUS[k].label.toLowerCase()}</span>`).join(" &nbsp;"));
}

/* ---------- Isometric cluster ---------- */

function manifestEntries() {
  return [...(report?.workloads || []).flatMap((w) => Object.entries(w.manifests || {})),
    ...Object.entries(report?.node_pool?.migrations || {})];
}

function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type})), a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function refreshReports() {
  const data = await api("/api/reports");
  reports = data.runs || [];
  latestRun = reports[0]?.run || runId;
  $("#report-select").innerHTML = reports.length ? reports.map((r, i) => `<option value="${esc(r.run)}">${i === 0 ? "Latest · " : ""}${esc(r.run)} · ${esc(r.mode || "analysis")}</option>`).join("")
    : '<option value="">No analysis yet</option>';
  $("#report-select").value = runId || "";
  updateControls();
}

function renderPlan() {
  const el = $("#plan"), np = report?.node_pool;
  if (!report) { el.textContent = ""; return; }
  const changed = report.workloads.filter((w) => w.recommendation).length;
  const clamps = report.workloads.reduce((n, w) => n + (w.guardrail_notes || []).length, 0);
  el.innerHTML = `<details class="plan-summary"><summary>Review the plan · ${changed} workloads · ${manifestEntries().length} manifests · ${clamps} guardrail adjustments</summary>
    <div class="plan-grid"><div><p class="muted">Machine placement · cloud pricing model</p>
      ${np ? `<div class="plan-route">${esc(np.before_nodes)} × ${esc(np.before_sku)} → ${esc(np.choice.nodes)} × ${esc(np.choice.sku)}</div>
      <p>${esc(np.choice.rationale)}</p>${(np.notes || []).length ? `<ul>${np.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}` : "<p>No machine-pool change proposed.</p>"}</div>
      <div><p><b>Before you apply</b></p><p>Validate only asks Kubernetes to check the manifests without saving changes. Apply writes the selected run to the cluster.</p>
      <p>Resource guardrails use observed usage to set a floor. A workload can cost more when its measured needs or scaling limits require extra capacity.</p>
      <p>Moving pods does not switch off a machine or reduce a real cloud bill automatically.</p></div></div>
    ${np && Object.keys(np.migrations || {}).length ? `<details class="files"><summary>Machine migration manifests (${Object.keys(np.migrations).length})</summary><pre>${Object.entries(np.migrations).map(([f, t]) => `<span class="file"># ${esc(f)}</span>\n${esc(t)}`).join("\n")}</pre></details>` : ""}</details>`;
}

function renderClusterStatus() {
  const c = cluster, el = $("#cluster-status");
  if (!c?.available) {
    el.innerHTML = `${icon("complex")}<strong>${c ? "Cluster not reachable" : "Connecting to cluster…"}</strong><span class="muted">${esc(c?.error || "Live data is unavailable. Analysis reports remain available.")}</span>`;
    return;
  }
  const nodes = c.nodes.filter((n) => n.role !== "pending"), pods = nodes.flatMap((n) => n.pods).filter(holding);
  const pending = c.nodes.flatMap((n) => n.pods).filter((p) => p.phase === "Pending").length;
  el.innerHTML = `<strong>${icon(nodes.every((n) => n.ready) ? "healthy" : "complex")} ${nodes.filter((n) => n.ready).length}/${nodes.length} machines ready</strong>
    <span>${pods.filter((p) => p.ready).length}/${pods.length} pods ready${pending ? ` · ${pending} pending` : ""}</span>
    ${c.objects === false ? `<strong>${icon("complex")} Pods and workloads could not be read; the counts are unknown, not zero</strong>` : ""}
    <span class="muted">${c.metrics ? "Live metrics" : "Usage metrics unavailable"} · refreshed ${esc(new Date(c.observed_at || Date.now()).toLocaleTimeString())} · polls every 3s</span>
    ${nodes.some((n) => n.k3d) ? '<span class="muted">Desktop usage covers its Kubernetes container only.</span>' : ""}
    ${c.operation ? `<strong>${icon("loader", "spin")} ${esc(c.operation.name)} in progress</strong>` : ""}`;
}

const ISO = {slot: 34, gap: 14, margin: 18, cols: 3, slab: 14, zpc: 46, nodeGap: 120};  // zpc: height of one CPU core
const COS30 = Math.cos(Math.PI / 6);
const project = (x, y, z = 0) => [(x - y) * COS30, (x + y) / 2 - z];
const ptsOf = (...ps) => ps.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
function prism(x, y, w, d, z0, z1, P = project) {
  const A = P(x, y, z1), B = P(x + w, y, z1), C = P(x + w, y + d, z1), D = P(x, y + d, z1);
  const A0 = P(x, y, z0), B0 = P(x + w, y, z0), C0 = P(x + w, y + d, z0), D0 = P(x, y + d, z0);
  return {top: ptsOf(A, B, C, D), right: ptsOf(B0, C0, C, B), left: ptsOf(D0, C0, C, D),
          back: `M${ptsOf(A0)}L${ptsOf(B0)}M${ptsOf(A0)}L${ptsOf(D0)}M${ptsOf(A0)}L${ptsOf(A)}`};
}
const cpuName = (m) => (m || "").replace(/^AMD |^Intel\(R\) Core\(TM\) /, "").replace(/ (w\/|with) .*$| \d+-Core Processor$/, "");
const coreH = new Map();  // pod -> core height on screen, so a new usage reading animates instead of jumping

function tweenCores(tweens) {
  const moving = reduceMotion.matches ? [] : tweens.filter((t) => Math.abs(t.to - t.from) > 0.5);
  tweens.forEach((t) => coreH.set(t.name, t.to));
  if (!moving.length) return;
  const t0 = performance.now(), dur = 900;
  const frame = (now) => {
    const k = Math.min(1, (now - t0) / dur), e = 1 - (1 - k) ** 3;
    moving.forEach((t) => {
      const g = $("#scene").querySelector(`[data-pod="${CSS.escape(t.name)}"] .core`);
      if (!g) return;
      const f = prism(t.x, t.y, t.s, t.s, 0, t.from + (t.to - t.from) * e);
      g.children[0].setAttribute("points", f.left); g.children[1].setAttribute("points", f.right); g.children[2].setAttribute("points", f.top);
    });
    if (k < 1) requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

function renderKey() {
  const glass = `<svg viewBox="-12 -16 24 30"><polygon points="0,-14 10,-8 10,6 0,12 -10,6 -10,-8" style="fill:var(--glass);stroke:var(--wire);stroke-opacity:.7"/>
    <path d="M-10,-8 0,-2 10,-8M0,-2V12" style="fill:none;stroke:var(--wire);stroke-opacity:.7"/></svg>`;
  const core = `<svg viewBox="-12 -16 24 30"><polygon points="0,0 7,4 7,10 0,14 -7,10 -7,4" style="fill:var(--fg)" filter="url(#glow)"/></svg>`;
  const leds = `<svg viewBox="0 0 30 12" style="width:30px">${[0, 1, 2, 3, 4].map((i) =>
    `<rect x="${i * 6}" y="3" width="4" height="6" rx="1" style="fill:${i < 2 ? "var(--rec)" : "var(--track)"}"/>`).join("")}</svg>`;
  $("#key").innerHTML = `<span>${glass}CPU a pod reserves</span><span>${core}CPU it uses now</span>
    <span>${leds}Machine CPU now, 10% per light</span>`;
}

function renderCluster() {
  const c = cluster, scene = $("#scene"), svg = $("#iso"), empty = $("#cluster-empty");
  if (!c || !c.available) {
    scene.innerHTML = "";
    svg.setAttribute("viewBox", "0 0 10 1");
    empty.hidden = false;
    empty.textContent = c ? "Cluster not reachable. Reports are still available; live actions resume when the connection returns." : "Connecting…";
    return;
  }
  if (!c.nodes.length) { scene.innerHTML = ""; empty.hidden = false; empty.textContent = "No machines found in this cluster."; return; }
  empty.hidden = true;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const P = (x, y, z = 0) => {
    const p = [(x - y) * COS30, (x + y) / 2 - z];
    minX = Math.min(minX, p[0]); maxX = Math.max(maxX, p[0]); minY = Math.min(minY, p[1]); maxY = Math.max(maxY, p[1]);
    return p;
  };
  const pts = ptsOf, box = (x, y, w, d, z0, z1) => prism(x, y, w, d, z0, z1, P), tweens = [];
  const slotXY = (ox, oy, k) => [ox + ISO.margin + (k % ISO.cols) * (ISO.slot + ISO.gap),
                                 oy + ISO.margin + Math.floor(k / ISO.cols) * (ISO.slot + ISO.gap)];
  const W = ISO.margin * 2 + ISO.cols * ISO.slot + (ISO.cols - 1) * ISO.gap;
  const column = $(".stage-wrap").clientWidth < 560 || c.nodes.length > 2;
  let T = 0, prevD = 0, prevHeight = 2 * ISO.zpc, out = "";
  c.nodes.forEach((n, i) => {
    const pods = [...n.pods].sort((a, b) => a.app.localeCompare(b.app) || a.name.localeCompare(b.name));
    const rows = Math.max(2, Math.ceil(pods.length / ISO.cols));
    const D = ISO.margin * 2 + rows * ISO.slot + (rows - 1) * ISO.gap;
    // Origin (T, -T) moves along the screen's horizontal, (T, T) straight down: below the previous labels,
    // leaving headroom for 2-core pods.
    const maxHeight = Math.max(2 * ISO.zpc, ...pods.map((p) => Math.max(p.cpu_request_m || 0, p.cpu_used_m || 0) / 1000 * ISO.zpc));
    if (i) T += column ? (W + prevD) / 2 + ISO.slab + 100 + Math.max(maxHeight, prevHeight) - ISO.margin : (W + prevD + D + ISO.nodeGap) / 2;
    prevD = D;
    prevHeight = maxHeight;
    const ox = T, oy = column ? T : -T, slab = box(ox, oy, W, D, -ISO.slab, 0);
    const live = pods.filter(holding);
    const reqM = sum(live, "cpu_request_m"), usedM = measured(live, "cpu_used_m");
    let g = `<g class="${n.role === "pending" ? "pending-node" : ""}" data-node="${esc(n.name)}" data-tip tabindex="0" role="button"
      aria-label="${esc(`Machine ${n.host || n.name}, ${pods.length} pods`)}">
      <polygon class="slab-l" points="${slab.left}"/><polygon class="slab-r" points="${slab.right}"/><polygon class="slab-t" points="${slab.top}"/>`;
    for (let v = 0; v < 4; v++) {  // vents on the right face, a little fine-line hardware
      const x = ox + W, y = oy + D - 22 - v * 7;
      g += `<path class="vent" d="M${pts(P(x, y, -ISO.slab + 4))}L${pts(P(x, y, -4))}"/>`;
    }
    const led = (x0, x1) => pts(P(ox + x0, oy + D, -9.5), P(ox + x1, oy + D, -9.5), P(ox + x1, oy + D, -4.5), P(ox + x0, oy + D, -4.5));
    g += `<polygon class="led ${n.ready ? "" : "off"}" points="${led(12, 24)}"/><polygon class="led ${live.length ? "" : "off"}" points="${led(30, 42)}"/>`;
    const load = n.cpu_used_m != null && n.cpu_cores ? n.cpu_used_m / (n.cpu_cores * 1000) : null;
    for (let k = 0; k < 10; k++) {  // the machine's CPU right now (metrics-server), one light per 10%
      const on = load != null && k < Math.max(load > 0 ? 1 : 0, Math.round(load * 10));
      g += `<polygon class="led load ${on ? "" : "off"}" points="${led(54 + k * 10, 61 + k * 10)}"/>`;
    }
    for (let k = 0; k < rows * ISO.cols; k++) {
      const [x, y] = slotXY(ox, oy, k);
      g += `<polygon class="socket" points="${box(x, y, ISO.slot, ISO.slot, 0, 0).top}"/>`;
    }
    pods.forEach((p, k) => {
      const [x, y] = slotXY(ox, oy, k);
      const term = p.phase === "Terminating", pend = p.phase === "Pending";
      let hR = p.cpu_request_m != null ? Math.max(3, (p.cpu_request_m / 1000) * ISO.zpc) : 0;
      const hU = p.cpu_used_m != null && !pend ? Math.max(1.5, (p.cpu_used_m / 1000) * ISO.zpc) : 0;
      if (!hR && !hU) hR = 3;
      const glass = hR ? box(x, y, ISO.slot, ISO.slot, 0, hR) : null, inset = 7, s = ISO.slot - 2 * inset;
      const from = coreH.has(p.name) ? coreH.get(p.name) : hU;  // drawn at the last reading, then tweened to this one
      tweens.push({name: p.name, x: x + inset, y: y + inset, s, from, to: hU});
      box(x + inset, y + inset, s, s, 0, hU);  // keeps the frame big enough for where the tween ends
      const core = from || hU ? box(x + inset, y + inset, s, s, 0, from) : null;
      const glassSvg = glass ? `<g class="glass"><polygon points="${glass.left}"/><polygon points="${glass.right}"/><polygon points="${glass.top}"/></g>` : "";
      const coreSvg = core ? `<g class="core"><polygon class="cf-l" points="${core.left}"/><polygon class="cf-r" points="${core.right}"/><polygon class="cf-t" points="${core.top}"/></g>` : "";
      const cls = ["pod", p.ready && !term ? "lit" : "", pend ? "pending" : "", term ? "terminating" : "",
                   !p.ready && !pend && !term ? "notready" : "", seenPods.has(p.name) ? "" : "enter"];
      g += `<g class="${cls.join(" ")}" data-pod="${esc(p.name)}" data-app="${esc(p.app)}" data-tip tabindex="0" role="button"
        aria-label="${esc(`${p.app} pod on ${shortNode(n.name)}`)}" style="--c:${color(p.app)};animation-delay:${seenPods.size ? 0 : 120 + k * 60}ms">
        ${glass ? `<path class="glass-back" d="${glass.back}"/>` : ""}${hU > hR ? glassSvg + coreSvg : coreSvg + glassSvg}</g>`;
    });
    const cx = (ox - oy + W / 2 - D / 2) * COS30, by = (ox + oy + W + D) / 2 + ISO.slab;
    const hw = n.role === "pending" ? "waiting for a machine" : [cpuName(n.cpu_model), n.cpu_cores != null ? `${n.cpu_cores} threads` : "",
      n.mem_mi != null ? `${mib(n.mem_mi)} RAM` : ""].filter(Boolean).join(" · ");
    const now = n.cpu_used_m != null ? `using ${cores(n.cpu_used_m)} cores · ${mib(n.mem_used_mi)}${n.k3d ? " (k3d node)" : ""}` : "";
    const use = live.length ? `pods reserve ${cores(reqM)} cores, use ${cores(usedM)}${coverage(live, "cpu_used_m") ? ` (${coverage(live, "cpu_used_m")})` : ""}` : "no workload pods";
    const tag = n.role === "control-plane" ? "control plane" : n.role;
    g += `<text class="nl-name" x="${cx.toFixed(1)}" y="${by + 28}">${esc(n.host || shortNode(n.name))}<tspan class="nl-tag" dx="8">${esc(tag)}</tspan></text>
      <text class="nl-sub" x="${cx.toFixed(1)}" y="${by + 46}">${esc(hw)}</text>
      <text class="nl-sub" x="${cx.toFixed(1)}" y="${by + 63}">${esc(now)}</text>
      <text class="nl-sub" x="${cx.toFixed(1)}" y="${by + 80}">${esc(use)}</text></g>`;
    const labelHalf = Math.max(140, hw.length * 3.2, (n.host || n.name).length * 4.5 + tag.length * 3.5);
    minX = Math.min(minX, cx - labelHalf); maxX = Math.max(maxX, cx + labelHalf); maxY = Math.max(maxY, by + 86);
    out += g;
  });
  minY = Math.min(minY, ISO.margin - 2 * ISO.zpc - 8);  // headroom for 2-core pods, so the frame doesn't jump
  const pad = 10;
  svg.setAttribute("viewBox", `${(minX - pad).toFixed(0)} ${(minY - pad).toFixed(0)} ${(maxX - minX + 2 * pad).toFixed(0)} ${(maxY - minY + 2 * pad).toFixed(0)}`);
  replaceHTML(scene, out);
  c.nodes.forEach((n) => n.pods.forEach((p) => seenPods.add(p.name)));
  tweenCores(tweens);
  applyFocus();
}

function findPod(name) {
  for (const n of cluster?.nodes || []) for (const p of n.pods) if (p.name === name) return {...p, node: n.name};
  return null;
}

function describeIso(t) {
  if (t.dataset.pod) {
    const p = findPod(t.dataset.pod);
    if (!p) return {title: t.dataset.pod, rows: []};
    return {title: p.app, sub: `${p.name} · ${shortNode(p.node)}`, rows: [
      ["CPU used / reserved", `${cores(p.cpu_used_m)} / ${cores(p.cpu_request_m)} cores`],
      ["Memory used / reserved", `${mib(p.mem_used_mi)} / ${mib(p.mem_request_mi)}`],
      ["Phase", `${p.phase}${p.ready ? ", ready" : ""}`], ["Restarts", String(p.restarts ?? 0)]]};
  }
  const n = cluster.nodes.find((x) => x.name === t.dataset.node);
  if (!n) return {title: t.dataset.node, rows: []};
  return {title: n.host || shortNode(n.name), sub: `${n.name} · ${n.role}${n.k3d ? " · k3d container" : ""}`, rows: [
    ["CPU", `${cpuName(n.cpu_model) || "—"} · ${n.cpu_cores ?? "?"} threads`], ["Memory", mib(n.mem_mi)],
    ["Using now", n.cpu_used_m != null ? `${cores(n.cpu_used_m)} cores · ${mib(n.mem_used_mi)}` : "—"],
    ["Workload pods", String(n.pods.length)], ["Cost model", n.pool ? `${n.pool} · ${usd(n.pool_monthly_usd ?? 0)}/mo` : "—"]]};
}

function renderLegend() {
  const counts = {};
  (cluster?.nodes || []).forEach((n) => n.pods.forEach((p) => { if (holding(p)) counts[p.app] = (counts[p.app] || 0) + 1; }));
  (report?.workloads || []).forEach((w) => (counts[appOf(w.id)] ??= 0));
  const apps = Object.keys(counts).sort((a, b) => order.indexOf(a) - order.indexOf(b) || a.localeCompare(b));
  apps.forEach(color);
  replaceHTML($("#legend"), apps.map((a) => `<button class="chip" data-app="${esc(a)}" aria-pressed="false">
    <span class="dot" style="--c:${color(a)}"></span>${esc(a)}<span class="n">${counts[a]} pod${counts[a] === 1 ? "" : "s"}</span></button>`).join(""));
  applyFocus();
}

/* ---------- Inspector ---------- */

function meter(label, used, req, cap, fmt) {  // req null: a plain usage meter
  const pct = (v) => Math.min(100, ((v || 0) / cap) * 100).toFixed(1);
  const text = req == null ? `${fmt(used)} of ${fmt(cap)}` : `${fmt(req)} reserved · ${fmt(used)} used of ${fmt(cap)}`;
  return `<div class="meter"><div class="meter-h"><span>${label}</span><span>${text}</span></div>
    <div class="bar">${req == null ? "" : `<span class="req" style="width:${pct(req)}%"></span>`}<span class="use" style="width:${pct(used)}%"></span></div></div>`;
}
const closeBtn = `<button class="btn btn-ghost btn-icon btn-sm" data-close aria-label="Back to the whole cluster">${icon("x")}</button>`;

function renderInspector() {
  const el = $("#inspector"), c = cluster;
  if (!c || !c.available) { replaceHTML(el, `<p class="muted">${c ? "Live inspection is unavailable while the cluster is disconnected." : "Waiting for the cluster…"}</p>`); return; }
  const pods = c.nodes.flatMap((n) => n.pods.map((p) => ({...p, node: n.name}))).filter(holding);
  if (sel?.kind === "node") {
    const n = c.nodes.find((x) => x.name === sel.id);
    if (n) { replaceHTML(el, nodePanel(n, pods.filter((p) => p.node === n.name))); return; }
  }
  if (sel?.kind === "app") { replaceHTML(el, appPanel(sel.id, pods.filter((p) => p.app === sel.id))); return; }
  const req = sum(pods, "cpu_request_m"), used = measured(pods, "cpu_used_m");
  const apps = [...new Set(pods.map((p) => p.app))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
  replaceHTML(el, `<div class="insp-h"><b>Whole cluster</b></div>
    <div class="big">${c.metrics && req && used != null ? Math.round((used / req) * 100) + "%" : "—"}</div>
    <div class="muted">${!c.metrics ? "live usage appears once metrics-server is ready"
      : used == null ? `waiting for usage samples: ${coverage(pods, "cpu_used_m")}` : "of the CPU reserved by workload pods is in use right now"}</div>
    <dl class="kv"><dt>Machines</dt><dd>${esc(c.nodes.filter((n) => n.role !== "pending").map((n) => n.host || n.name).join(", "))}</dd>
      <dt>Real capacity</dt><dd>${sum(c.nodes, "cpu_cores")} threads · ${mib(sum(c.nodes, "mem_mi"))}</dd>
      <dt>Cost model</dt><dd>${usd(sum(c.nodes, "pool_monthly_usd"))}/mo</dd>
      <dt>CPU reserved · used</dt><dd>${cores(req)} · ${cores(used)} cores${partial(pods, "cpu_used_m")}</dd>
      <dt>Memory reserved · used</dt><dd>${mib(sum(pods, "mem_request_mi"))} · ${mib(measured(pods, "mem_used_mi"))}${partial(pods, "mem_used_mi")}</dd></dl>
    <table class="mini"><tr><th>Workload</th><th>Pods</th><th>Reserved</th><th>Used</th></tr>${apps.map((a) => {
      const ps = pods.filter((p) => p.app === a);
      return `<tr><td><button class="linkish" data-pick="${esc(a)}"><span class="dot" style="--c:${color(a)}"></span>${esc(a)}</button></td>
        <td>${ps.length}</td><td>${cores(sum(ps, "cpu_request_m"))}</td><td>${cores(measured(ps, "cpu_used_m"))}</td></tr>`;
    }).join("")}</table>`);
}

function nodePanel(n, pods) {
  if (n.role === "pending") return `<div class="insp-h"><b>Unscheduled pods</b>${closeBtn}</div><p class="muted">No machine has room for these yet.</p>`;
  const tags = [n.role === "control-plane" ? "control plane" : n.role, n.k3d ? "k3d container" : "bare metal", n.ready ? "Ready" : "Not ready"]
    .map((t) => `<span class="badge">${esc(t)}</span>`).join("");
  const where = n.k3d ? "k3d node" : "Machine";
  return `<div class="insp-h"><b>${esc(n.host || shortNode(n.name))}</b>${closeBtn}</div><div class="insp-tags">${tags}</div>
    <dl class="kv"><dt>CPU</dt><dd>${esc(cpuName(n.cpu_model) || "—")}</dd><dt>Threads · RAM</dt><dd>${n.cpu_cores ?? "—"} · ${mib(n.mem_mi)}</dd>
      <dt>OS</dt><dd>${esc(n.os || "—")}</dd><dt>Kernel</dt><dd>${esc(n.kernel || "—")}</dd>
      <dt>Kubernetes</dt><dd>${esc(n.kubelet || "—")}</dd><dt>IP</dt><dd>${esc(n.external_ip || n.internal_ip || "—")}</dd>
      <dt>Node</dt><dd>${esc(n.name)}</dd><dt>Joined</dt><dd>${n.created ? esc(new Date(n.created).toLocaleString()) : "—"}</dd></dl>
    ${n.alloc_cpu_m ? meter("Pods: CPU", measured(pods, "cpu_used_m"), sum(pods, "cpu_request_m"), n.alloc_cpu_m, cores)
      + meter("Pods: memory", measured(pods, "mem_used_mi"), sum(pods, "mem_request_mi"), n.alloc_mem_mi, mib)
      + (coverage(pods, "cpu_used_m") ? `<p class="muted">Usage total waits for every pod: ${coverage(pods, "cpu_used_m")}.</p>` : "") : ""}
    ${n.cpu_used_m != null ? meter(`${where}: CPU now`, n.cpu_used_m, null, n.cpu_cores * 1000, cores)
      + meter(`${where}: memory now`, n.mem_used_mi, null, n.mem_mi, mib) : ""}
    <p class="muted">${n.k3d ? "This node is a k3d container on the desktop, so its usage is Kubernetes' share, not everything the desktop runs."
      : "Usage is the whole machine, including what runs outside Kubernetes."}${pods.length ? "" : " No workload pods here right now."}</p>
    <p class="note">Cost model: labelled <b>${esc(n.pool || "none")}</b>${n.pool_monthly_usd != null ? `, ${usd(n.pool_monthly_usd)}/mo` : ""} in
      config/skus.yaml, the cloud VM this machine stands in for. It is not the price of the hardware.</p>`;
}

function appPanel(app, pods) {
  const w = report?.workloads.find((x) => appOf(x.id) === app), sc = cluster.scaling[app] || {}, rec = w?.recommendation;
  const deployment = cluster.deployments?.[app], livePlan = cluster.plan?.run === runId ? cluster.plan.workloads?.[app] : null;
  const differences = livePlan?.checks.filter((c) => !c.match) || [];
  const tags = [w ? statusBadge(w.triage.status) : "", w?.tier ? `<span class="badge">${esc(TIER[w.tier])}</span>` : ""].join("");
  const scaling = sc.min != null ? `KEDA ${sc.min}–${sc.max} · ${esc((sc.triggers || []).join(" + "))}` : "fixed replicas";
  return `<div class="insp-h"><span class="dot" style="--c:${color(app)}"></span><b>${esc(app)}</b>${closeBtn}</div><div class="insp-tags">${tags}</div>
    <dl class="kv"><dt>Pods ready</dt><dd>${pods.filter((p) => p.ready).length} of ${pods.length}${sc.desired != null && sc.desired !== pods.length ? ` → ${sc.desired}` : ""}</dd>
      <dt>Scaling</dt><dd>${scaling}</dd>${sc.cpu != null ? `<dt>CPU vs target</dt><dd>${sc.cpu}% / ${sc.cpu_target ?? "—"}%</dd>` : ""}
      <dt>CPU reserved · used</dt><dd>${cores(sum(pods, "cpu_request_m"))} · ${cores(measured(pods, "cpu_used_m"))}${partial(pods, "cpu_used_m")}</dd>
      <dt>Memory reserved · used</dt><dd>${mib(sum(pods, "mem_request_mi"))} · ${mib(measured(pods, "mem_used_mi"))}${partial(pods, "mem_used_mi")}</dd>
      ${deployment ? `<dt>Live request / pod</dt><dd>${cores(deployment.cpu_request_m)} cores · ${mib(deployment.mem_request_mi)}</dd>` : ""}
      ${livePlan ? `<dt>Plan vs live specs</dt><dd>${livePlan.state === "applied" ? "Matches" : `${differences.length} differences`}</dd>` : ""}
      ${w ? `<dt>Analysis cost estimate</dt><dd>${usd(w.monthly_before_usd)} → ${usd(w.monthly_after_usd)}/mo</dd>` : ""}</dl>
    ${differences.length ? `<details class="files" data-preserve="drift"><summary>View differences from this plan</summary><ul>${differences.map((c) => `<li><b>${esc(c.field)}</b><br>Live: ${esc(JSON.stringify(c.actual) ?? "unset")}<br>Planned: ${esc(JSON.stringify(c.expected) ?? "unset")}</li>`).join("")}</ul></details>` : ""}
    ${rec ? `<p class="note">${esc(rec.diagnosis)}</p>` : w ? `<p class="note">${esc(w.triage.summary)}</p>` : ""}
    ${pods.length ? `<table class="mini" style="margin-top:10px"><tr><th>Pod</th><th>Node</th><th>Used / reserved</th></tr>${pods.map((p) =>
      `<tr><td>${esc(shortPod(p.name, app))}</td><td>${esc(shortNode(p.node))}</td><td>${cores(p.cpu_used_m)} / ${cores(p.cpu_request_m)}</td></tr>`).join("")}</table>`
      : `<p class="muted">No pods right now${sc.min === 0 ? " (scaled to zero until its next cron window)" : ""}.</p>`}`;
}

/* ---------- Reserved vs used (bullet charts) ---------- */

function niceMax(v) {
  if (!(v > 0)) return 1;
  const e = 10 ** Math.floor(Math.log10(v)), m = v / e;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * e;
}

function bullet(rows, key, title, fmt, W) {
  const compact = W < 480, L = compact ? 8 : 128, R = compact ? 12 : 136, rowH = compact ? 58 : 36, top = 6, H = top + rows.length * rowH + 22;
  const max = niceMax(Math.max(...rows.flatMap((r) => [r[key].req, r[key].used, r[key].rec ?? 0])));
  const x = (v) => L + (Math.min(v, max) / max) * (W - L - R);
  let s = "";
  [0, max / 2, max].forEach((t) => {
    s += `<line class="gridline" x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${H - 18}"/>
      <text class="tick" x="${x(t)}" y="${H - 4}" text-anchor="${t === 0 ? "start" : t === max ? "end" : "middle"}">${fmt(t)}</text>`;
  });
  rows.forEach((r, i) => {
    const v = r[key], y = top + i * rowH + (compact ? 38 : rowH / 2), labelY = compact ? y - 21 : y + 4;
    s += `<g class="brow" data-app="${esc(r.app)}" data-key="${key}" data-tip tabindex="0" role="button"
      aria-label="${esc(`Inspect ${r.app}, ${title}: reserved ${fmt(v.req)}, used ${fmt(v.used)}${v.rec != null ? `, recommended ${fmt(v.rec)}` : ""}`)}">
      <rect class="row-hit" x="1" y="${top + i * rowH + 1}" width="${W - 2}" height="${rowH - 2}" rx="6"/>
      <circle cx="8" cy="${labelY - 4}" r="4" style="fill:${color(r.app)}"/><text class="lbl" x="18" y="${labelY}">${esc(r.app)}</text>
      <path class="trk" d="${hbar(L, y - 7, x(v.req) - L, 14)}"/>
      <path class="usebar" d="${hbar(L, y - 3, x(v.used) - L, 6, 3)}"/>
      ${v.rec != null ? `<line class="recmark" x1="${x(v.rec)}" x2="${x(v.rec)}" y1="${y - 10}" y2="${y + 10}"/>` : ""}
      <text class="val" x="${compact ? W - 8 : W - R + 10}" y="${labelY}" ${compact ? 'text-anchor="end"' : ""}>${fmt(v.req)}${v.rec != null ? " → " + fmt(v.rec) : ""}</text></g>`;
  });
  return `<figure class="chart"><figcaption>${title}</figcaption>
    <svg viewBox="0 0 ${W} ${H}" role="group" aria-label="${title}: reserved, used and recommended per workload">${s}</svg></figure>`;
}

function barRows() {
  const stats = Object.fromEntries(report.stats.map((s) => [s.id, s]));
  return report.workloads.map((w) => {
    const s = stats[w.id], res = w.recommendation?.resources;
    return {app: appOf(w.id), w, s,
      cpu: {req: s.cpu_request_cores ?? 0, used: s.cpu_pod_p95, rec: res ? res.cpu_request_m / 1000 : null},
      mem: {req: (s.mem_request_mib ?? 0) / 1024, used: s.mem_pod_p99_mib / 1024, rec: res ? res.mem_request_mi / 1024 : null}};
  });
}

function renderBars() {
  const el = $("#bars");
  if (!report) { el.innerHTML = `<p class="empty">Run an analysis to compare what each workload reserves with what it uses.</p>`; return; }
  const rows = barRows(), cw = el.clientWidth || 480, two = cw >= 720, W = Math.max(300, Math.floor(two ? (cw - 32) / 2 : cw));
  el.classList.toggle("two", two);
  el.innerHTML = bullet(rows, "cpu", "CPU per pod, cores", (v) => v.toFixed(2), W)
    + bullet(rows, "mem", "Memory per pod", (v) => (v < 1 ? Math.round(v * 1024) + " Mi" : (+v.toFixed(1)) + " Gi"), W);
  applyFocus();
}

function describeBar(t) {
  const r = barRows().find((x) => x.app === t.dataset.app), cpu = t.dataset.key === "cpu";
  const f = cpu ? (v) => v.toFixed(2) + " cores" : (v) => mib(v * 1024), v = r[t.dataset.key];
  return {title: r.app, sub: cpu ? "CPU per pod" : "Memory per pod", rows: [
    ["Reserved at analysis", f(v.req)], [cpu ? "Used, p95" : "Used, p99", f(v.used)],
    ["Recommended", v.rec != null ? f(v.rec) : "no change"], ["Pods", String(r.s.replicas)]]};
}

/* ---------- How it decided (router flow) ---------- */

function renderFlow(animate) {
  const el = $("#flow");
  if (!report) {
    el.innerHTML = `<p class="empty">The model router appears here after an analysis.</p>`;
    $("#totals").textContent = ""; $("#ledger").textContent = "";
    return;
  }
  const r = report, groups = ["strategist", "worker", "stop"];
  const outcome = (w) => w.tier || "stop";
  const rows = [...r.workloads].sort((a, b) => groups.indexOf(outcome(a)) - groups.indexOf(outcome(b)));
  const W = Math.max(320, Math.min(620, el.clientWidth || 520)), rowH = 30, top = 30, H = top + rows.length * rowH + 10;
  const xL = 120, xO = W - (W < 440 ? 128 : 168), xT = Math.round(xL + (xO - xL) * 0.42);
  const calls = {};
  r.ledger.forEach((e) => { const k = e.stage === "triage" ? "triage" : e.tier; (calls[k] ||= []).push(e); });
  const live = groups.filter((g) => rows.some((w) => outcome(w) === g));
  const per = 20, gapG = 18, total = rows.length * per + (live.length - 1) * gapG;
  let yo = top + (rows.length * rowH - total) / 2;
  const slot = {}, bars = [];
  live.forEach((g) => {
    const ws = rows.filter((w) => outcome(w) === g);
    bars.push({g, y0: yo, y1: yo + ws.length * per});
    ws.forEach((w, j) => (slot[w.id] = yo + per / 2 + j * per));
    yo += ws.length * per + gapG;
  });
  const tri = calls.triage?.[0];
  let s = `<text class="flow-col" x="${xL}" y="12" text-anchor="end">STATS · $0</text>
    <text class="flow-col" x="${xT + 4}" y="12" text-anchor="middle">TRIAGE</text><text class="flow-col" x="${xO}" y="12">FIX</text>
    <rect class="flow-bar" x="${xT}" y="${top}" width="8" height="${rows.length * rowH}" rx="4"/>`;
  rows.forEach((w, i) => {
    const y = top + i * rowH + rowH / 2, y2 = slot[w.id], app = appOf(w.id), mid = (xT + 8 + xO) / 2;
    const d = `M${xL + 8},${y}H${xT}M${xT + 8},${y}C${mid},${y} ${mid},${y2} ${xO},${y2}`;
    s += `<g class="wlf" data-app="${esc(app)}" data-wl="${esc(w.id)}" data-tip tabindex="0" role="button" aria-label="${esc(`Inspect ${app}: ${STATUS[w.triage.status].label}, ${w.tier ? TIER[w.tier] : "stopped at triage"}`)}" style="--c:${color(app)}">
      <text class="lbl" x="${xL - 4}" y="${y + 4}" text-anchor="end">${esc(app)}</text><circle cx="${xL + 4}" cy="${y}" r="4" style="fill:var(--c)"/>
      <path class="hit" d="${d}"/><path class="path" d="${d}" pathLength="1" style="animation-delay:${i * 90}ms"/></g>`;
  });
  bars.forEach(({g, y0, y1}) => {
    const es = calls[g] || [], cost = es.reduce((a, e) => a + e.cost_usd, 0), n = rows.filter((w) => outcome(w) === g).length;
    const name = g === "stop" ? "Stopped" : TIER[g];
    const sub = g === "stop" ? `${n} healthy · 0 tokens` : `${es.length} call${es.length === 1 ? "" : "s"} · ${usd(cost)}`;
    s += `<rect class="flow-bar ${g === "stop" ? "stop" : ""}" x="${xO}" y="${y0}" width="8" height="${y1 - y0}" rx="4"/>
      <text class="flow-out" x="${xO + 16}" y="${(y0 + y1) / 2 - 2}">${esc(name)}</text>
      <text class="val" x="${xO + 16}" y="${(y0 + y1) / 2 + 13}">${esc(sub)}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="group" aria-label="Model routing for each workload">${s}</svg>`;
  const inTok = sum(r.ledger, "input_tokens"), outTok = sum(r.ledger, "output_tokens"), b = r.baselines_usd;
  $("#totals").innerHTML = `<b>${r.ledger.length} calls · ${usd(r.llm_cost_usd)}</b> · ${tok(inTok)} in / ${tok(outTok)} out
    ${tri ? `· triage on ${esc(tri.model)}` : ""} <br><span class="muted">Same calls all on ${esc(TIER.strategist)}: ${usd(b.all_calls_on_strategist)} ·
    raw 5-minute series to ${esc(TIER.strategist)}: ${usd(b.raw_series_to_strategist)}</span>`;
  const who = (e) => (e.workload === "*" ? "all workloads" : e.workload === "cluster" ? "node pool" : appOf(e.workload));
  $("#ledger").innerHTML = `<table class="mini"><tr><th>Call</th><th>Tier</th><th>Tokens in / out</th><th>Cost</th></tr>${r.ledger.map((e) =>
    `<tr><td>${esc(e.stage)} · ${esc(who(e))}</td><td>${esc(TIER[e.tier] || e.tier)}</td><td>${tok(e.input_tokens)} / ${tok(e.output_tokens)}</td>
      <td>${usd(e.cost_usd)}</td></tr>`).join("")}
    <tr><td><b>Total</b></td><td></td><td>${tok(inTok)} / ${tok(outTok)}</td><td><b>${usd(r.llm_cost_usd)}</b></td></tr></table>`;
  applyFocus();
}

function describeFlow(t) {
  const w = report.workloads.find((x) => x.id === t.dataset.wl);
  const e = report.ledger.find((x) => x.stage === "recommend" && x.workload === w.id);
  return {title: appOf(w.id), sub: STATUS[w.triage.status].label + (w.tier ? ` → ${TIER[w.tier]}` : " → stopped at triage"), rows: e
    ? [["Model", e.model], ["Tokens in / out", `${tok(e.input_tokens)} / ${tok(e.output_tokens)}`], ["Cost", usd(e.cost_usd)]]
    : [["Extra tokens", "0"], ["Why", w.triage.summary.slice(0, 80)]]};
}

/* ---------- Recommendations ---------- */

function renderWorkloads() {
  const r = report;
  if (!r) return;
  const stats = Object.fromEntries(r.stats.map((s) => [s.id, s]));
  const calls = Object.fromEntries(r.ledger.filter((e) => e.stage === "recommend").map((e) => [e.workload, e]));
  const triShare = (r.ledger.find((e) => e.stage === "triage")?.cost_usd || 0) / Math.max(r.workloads.length, 1);
  const query = $("#workload-search").value.trim().toLowerCase(), filter = $("#status-filter").value;
  const visible = r.workloads.filter((w) => w.id.toLowerCase().includes(query) && (filter === "all" || filter === "changes" && !!w.recommendation || w.triage.status === filter));
  $("#workload-count").textContent = `${visible.length} of ${r.workloads.length}`;
  const open = new Set([...$$("details.wl[open]")].map((el) => el.dataset.id));
  $("#workloads").innerHTML = visible.map((w) => {
    const s = stats[w.id], rec = w.recommendation, e = calls[w.id], app = appOf(w.id);
    const delta = w.monthly_before_usd - w.monthly_after_usd, files = Object.entries(w.manifests);
    const money = rec ? `<s>${usd(w.monthly_before_usd)}</s> → <b>${usd(w.monthly_after_usd)}</b>/mo` : `<b>${usd(w.monthly_before_usd)}</b>/mo`;
    return `<details class="wl" data-id="${esc(w.id)}" ${open.has(w.id) ? "open" : ""}><summary><span class="dot" style="--c:${color(app)}"></span><span class="name">${esc(app)}</span>
        ${statusBadge(w.triage.status)}${w.tier ? `<span class="badge">${esc(TIER[w.tier])}</span>` : ""}
        ${rec ? `<span class="badge">risk ${esc(rec.risk)}</span>` : ""}<span class="spacer"></span><span class="money">${money}</span>${icon("chev", "chev")}</summary>
      <div class="wl-body">
        <p>${esc(rec ? rec.diagnosis : w.triage.summary)}</p>
        ${delta < 0 ? `<p><b>${usd(-delta)}/mo additional capacity.</b> The estimate includes the proposed resource requests and scaling schedule; review these alongside the guardrail adjustments.</p>` : ""}
        <p class="muted">CPU p95 ${s.cpu_pod_p95.toFixed(2)} of ${(s.cpu_request_cores ?? 0).toFixed(2)} cores · memory p99 ${Math.round(s.mem_pod_p99_mib)} of
          ${Math.round(s.mem_request_mib ?? 0)} MiB · ${s.replicas} pod${s.replicas === 1 ? "" : "s"} · ${s.restarts} restarts</p>
        ${rec && (w.guardrail_notes.length || rec.follow_ups.length) ? `<ul>${w.guardrail_notes.map((n) => `<li class="guard">${esc(n)}</li>`).join("")}
          ${rec.follow_ups.map((f) => `<li class="follow">${esc(f)}</li>`).join("")}</ul>` : ""}
        <div class="trace">${esc(TIER.triage)} share ${usd(triShare)}${e ? ` → ${esc(TIER[w.tier])} ${tok(e.input_tokens)} in / ${tok(e.output_tokens)} out ${usd(e.cost_usd)}
          · diagnosis ${usd(triShare + e.cost_usd)} · ${delta >= 0 ? `saves ${usd(delta)}/mo` : `costs ${usd(-delta)}/mo more`}` : " · stopped at triage"}</div>
        ${files.length ? `<details class="files"><summary>Manifests (${files.length})</summary><pre>${files.map(([f, t]) =>
          `<span class="file"># ${esc(f)}</span>\n${esc(t)}`).join("\n")}</pre><button class="btn btn-outline btn-sm" data-download="${esc(w.id)}">Download workload manifests</button></details>` : ""}
      </div></details>`;
  }).join("") || '<p class="empty">No workloads match. Try another name or status.</p>';
}

/* ---------- Steps ---------- */

function renderSteps() {
  const r = report, plan = cluster?.plan?.run === runId ? cluster.plan : null;
  const traffic = cluster?.traffic;
  if (traffic?.job === spikeJob && ["completed", "failed"].includes(traffic.status)) spikeEnds = 0;
  const jobs = cluster?.active_jobs || [], ends = Math.max(spikeEnds, ...jobs.map((j) => Date.parse(j.ends_at) || 0));
  const left = Math.max(0, Math.ceil((ends - Date.now()) / 1000)), loadgen = jobs.length > 0;
  const verified = plan?.state === "applied", validation = applied?.run === runId && applied.dry;
  $("#step-analyze").classList.toggle("done", !!r);
  $("#step-analyze").classList.toggle("next", !r);
  $("#st-analyze").textContent = r ? `Last run ${new Date(r.generated_at).toLocaleString()} · ${usd(r.llm_cost_usd)} · `
    + `${r.workloads.filter((w) => w.recommendation).length} of ${r.workloads.length} workloads get changes.` : "Not run yet.";
  $("#step-apply").classList.toggle("done", verified);
  $("#step-apply").classList.toggle("next", !!r && !verified);
  let applyText = !r ? "Analyze first to create a plan." : !cluster?.available ? "Connect the cluster to validate or apply." :
    verified ? `Live specs match this plan. ${plan.ready ? "Workloads are ready." : "Waiting for the rollout to finish."}` :
    plan?.state === "partial" ? `${plan.matched} of ${plan.total} workload plans fully match live specs. Review differences in the inspector.` :
    runId !== latestRun && latestRun ? "Viewing an older run. Apply uses this selected run; review its plan first." : "Review the plan, then validate or apply this run.";
  if (plan?.capacity && plan.capacity.ready < plan.capacity.required) applyText += ` Placement needs ${plan.capacity.required} ready ${plan.capacity.sku} machines; ${plan.capacity.ready} available. Migration will be skipped.`;
  if (validation) applyText += ` Validation: ${applied.ok}/${applied.total} checked${applied.skipped ? `, ${applied.skipped} skipped` : ""}. No changes saved.`;
  if (applied?.run === runId && !applied.dry && applied.skipped) applyText += ` Last apply skipped ${applied.skipped} steps; inspect Activity.`;
  $("#st-apply").textContent = applyText;
  $("#step-spike").classList.toggle("done", left > 0 || loadgen);
  const sc = cluster?.scaling?.["checkout-web"];
  const scalingText = sc?.max != null ? `${sc.current ?? "—"}/${sc.max} replicas · CPU ${sc.cpu ?? "—"}% / ${sc.cpu_target ?? "—"}% target.` : "Apply a scaling plan first to test autoscaling.";
  const ceiling = sc?.current >= sc?.max ? " At replica limit: CPU can rise without more pods." : "";
  $("#st-spike").textContent = (left > 0 ? `Traffic running · ${left}s left. ` : loadgen ? "Traffic job is starting or finishing. " : "Optional. ") + scalingText + ceiling;
  const load = (+$("#rps").value || 0) * (+$("#workms").value || 0) / 1000;
  $("#load-estimate").textContent = `About ${load.toFixed(1)} CPU cores of requested work for ${$("#secs").value}s. This consumes real machine CPU.`;
  const result = $("#traffic-result");
  result.hidden = !traffic;
  if (traffic) {
    const failed = traffic.status === "failed" || traffic.failed_requests > 0;
    const state = {pending: "Traffic queued", running: "Traffic running", completed: "Traffic finished", failed: "Traffic job failed"}[traffic.status] || "Traffic results";
    const samples = traffic.succeeded_requests != null;
    const html = `<b>${icon(failed ? "complex" : traffic.status === "completed" && traffic.succeeded_requests > 0 ? "healthy" : "zap")}${esc(state)}${traffic.failed_requests > 0 ? " · request errors" : ""}</b>
      ${samples ? `${traffic.succeeded_requests.toLocaleString()} successful · ${traffic.failed_requests.toLocaleString()} failed requests
        <small>Latest interval: p50 ${traffic.p50_ms.toLocaleString()} ms · p95 ${traffic.p95_ms.toLocaleString()} ms</small>`
        : '<span class="muted">Request results are unavailable. CPU and replica counts alone do not measure request success.</span>'}`;
    result.dataset.kind = failed ? "err" : "ok";
    if (result.innerHTML !== html) result.innerHTML = html;
  }
  updateControls();
}

/* ---------- Wiring ---------- */

function renderReport(animate) {
  const r = report;
  if (r) {
    TIER = {...TIER, ...(r.tiers || {})};
    r.workloads.map((w) => appOf(w.id)).sort().forEach(color);
    $("#runinfo").innerHTML = `<span class="badge">Run ${esc(runId)}</span>${r.window_days}-day window · namespace ${esc(r.namespace)} ·
      ${new Date(r.generated_at).toLocaleString()}`;
  }
  $("#snapshot-note").textContent = r ? `Analysis snapshot · ${runId} · ${r.window_days} days of history. Savings are modelled; live requests and usage appear in the cluster inspector.`
    : "Savings and recommendations are estimates from an analysis. The cluster below is live.";
  renderFigures(); renderPlan(); renderBars(); renderFlow(animate); renderWorkloads(); renderLegend(); renderInspector(); renderSteps();
  if (animate) sweep($("#headline"));
}

async function refreshCluster() {
  // A refresh asked for mid-poll (e.g. right after Apply) runs once that poll ends; that poll may predate the change.
  if (polling) { pollAgain = true; return; }
  polling = true;
  try {
    const c = await api("/api/cluster"), sig = JSON.stringify({...c, observed_at: undefined});
    cluster = c;
    renderClusterStatus(); renderSteps();
    if (sig === lastSig) return;
    lastSig = sig; cluster = c;
  } catch (e) {
    cluster = {available: false, error: "Cannot reach the cluster API. Check the server and Kubernetes connection."}; lastSig = "";
  } finally {
    polling = false;
    if (pollAgain) { pollAgain = false; setTimeout(refreshCluster, 0); }
  }
  renderCluster(); renderLegend(); renderInspector(); renderSteps(); renderClusterStatus();
}

function setLabel(id, iconName, text) { $(id).innerHTML = `${icon(iconName)}<span>${text}</span>`; }

function initTheme() {
  try { const t = localStorage.getItem("kubesage-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* storage blocked */ }
  const paint = () => {
    const dark = (document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
    $("#btn-theme").innerHTML = icon(dark ? "sun" : "moon");
    $("#btn-theme").setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    return dark;
  };
  $("#btn-theme").onclick = () => {
    const next = paint() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("kubesage-theme", next); } catch (e) { /* storage blocked */ }
    paint();
  };
  paint();
}

setLabel("#btn-analyze", "spark", "Analyze");
setLabel("#btn-apply", "check", "Apply fixes");
setLabel("#btn-gitops", "pr", "GitOps PR");
setLabel("#btn-reset", "undo", "Reset demo");
setLabel("#btn-spike", "zap", "Send traffic");
initTheme();
renderKey();

bindTips($("#iso"), describeIso, (t) => {
  if (!t) return select(null);
  select(t.dataset.pod ? {kind: "app", id: t.dataset.app} : {kind: "node", id: t.dataset.node});
});
bindTips($("#bars"), describeBar, (t) => t && select({kind: "app", id: t.dataset.app}));
bindTips($("#flow"), describeFlow, (t) => t && select({kind: "app", id: t.dataset.app}));
$("#legend").addEventListener("click", (ev) => { const c = ev.target.closest(".chip"); if (c) select({kind: "app", id: c.dataset.app}); });
$("#legend").addEventListener("pointerover", (ev) => { const c = ev.target.closest(".chip"); hoverApp = c ? c.dataset.app : null; applyFocus(); });
$("#legend").addEventListener("pointerleave", () => { hoverApp = null; applyFocus(); });
$("#inspector").addEventListener("click", (ev) => {
  if (ev.target.closest("[data-close]")) select(null);
  const pick = ev.target.closest("[data-pick]");
  if (pick) select({kind: "app", id: pick.dataset.pick});
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  hideTip();  // a tooltip from keyboard focus closes even when nothing is selected
  if (sel) select(null);
});

$("#btn-analyze").onclick = (ev) => busy(ev.currentTarget, async () => {
  notice("Analyzing usage history and checking the proposed changes…");
  log("analyzing: stats → triage → remediation → node pool");
  const res = await api("/api/analyze", {});
  // The plan on screen is the plan Apply submits: show it now, before a history refresh that can fail.
  runId = res.run; report = res.report; applied = null;
  renderReport(true);
  try { await refreshReports(); }
  catch (e) {
    log(`analysis history: ${e.message}`, "err");
    const select = $("#report-select");
    if (![...select.options].some((o) => o.value === runId)) select.add(new Option(`${runId} · ${report.mode || "analysis"}`, runId), 0);
    select.value = runId;
  }
  wireChanged();
  log(`analysis ${res.run} done · LLM ${usd(report.llm_cost_usd)}${report.mode === "mock" ? " (mock)" : ""}`, "ok");
  notice(`Analysis complete: ${report.workloads.filter((w) => w.recommendation).length} workloads have proposed changes. Review the plan before applying.`);
  await refreshCluster();
});
$("#btn-apply").onclick = (ev) => busy(ev.currentTarget, async () => {
  const dry = $("#dryrun").checked;
  notice(dry ? "Validating this plan with Kubernetes. No changes are saved…" : "Applying the selected plan. Live specs and rollout readiness will update below…");
  const res = await api("/api/apply", {dry_run: dry, run: runId});
  res.steps.forEach((s) => log(`${s.skipped ? "skip" : s.ok ? "ok" : "ERR"} ${s.file}: ${s.skipped || s.output}`, s.skipped ? "" : s.ok ? "ok" : "err"));
  const skipped = res.steps.filter((s) => s.skipped).length;
  log(`${dry ? "validated" : "applied"} ${res.run}${skipped ? ` · ${skipped} skipped` : ""}`, "ok");
  applied = {run: res.run, dry, skipped, ok: res.steps.filter((s) => s.ok && !s.skipped).length, total: res.steps.length};
  wireChanged();
  notice(`${dry ? "Validation complete. No changes saved." : "Apply finished; watching rollout readiness."} ${applied.ok}/${applied.total} changes ${dry ? "checked" : "written"}${skipped ? `; ${skipped} skipped. Open Activity for details.` : "."}`, skipped ? "warn" : "ok");
  renderSteps();
  await refreshCluster();
});
$("#btn-spike").onclick = (ev) => busy(ev.currentTarget, async () => {
  const inputs = [$("#rps"), $("#secs"), $("#workms")];
  if (inputs.some((el) => !el.reportValidity())) return;
  const res = await api("/api/spike", {rps: +$("#rps").value, work_ms: +$("#workms").value, seconds: +$("#secs").value});
  log(`spike ${res.job}: ${res.RPS} rps × ${res.WORK_MS} ms on ${res.TARGET} for ${res.SECONDS}s`, "ok");
  spikeEnds = Date.now() + +res.SECONDS * 1000;
  spikeJob = res.job;
  notice(`Traffic started: ${res.RPS} requests/s for ${res.SECONDS}s. Select checkout-web to watch CPU and scaling.`);
  renderSteps();
  await refreshCluster();
});
$("#btn-reset").onclick = (ev) => busy(ev.currentTarget, async () => {
  notice("Resetting demo workloads and removing demo scaling rules and traffic jobs…");
  const res = await api("/api/reset", {});
  applied = null; spikeEnds = 0; spikeJob = null;
  log("demo reset to the 'before' state", res.ok ? "ok" : "err");
  notice("Demo reset. Workloads are back to their original reservations and placement; reports remain available.");
  await refreshCluster();
});
$("#btn-gitops").onclick = (ev) => busy(ev.currentTarget, async () => {
  const res = await api("/api/gitops", {open_pr: true, run: runId});
  log(res.pr_url ? `PR opened: ${res.pr_url}` : `committed ${res.commit.slice(0, 10)} on ${res.branch}`, "ok");
});

$("#dismiss-notice").onclick = () => ($("#notice").hidden = true);
$("#toast-x").onclick = () => ($("#toast").hidden = true);
$("#dryrun").onchange = () => setLabel("#btn-apply", "check", $("#dryrun").checked ? "Validate plan" : "Apply fixes");
["#rps", "#secs", "#workms"].forEach((id) => $(id).addEventListener("input", renderSteps));
$("#report-select").onchange = (ev) => busy(ev.currentTarget, async () => {
  const selected = ev.currentTarget.value;
  try {
    const res = await api(`/api/report?run=${encodeURIComponent(selected)}`);
    runId = res.run; report = res.report; renderReport(false);
    notice(`Viewing ${selected === latestRun ? "the latest" : "a historical"} analysis: ${selected}. Live cluster data continues to refresh.`);
  } catch (e) { $("#report-select").value = runId || ""; throw e; }
});
$("#btn-export").onclick = () => report && download(`kubesage-${runId}.json`, JSON.stringify(report, null, 2) + "\n", "application/json");
$("#btn-manifests").onclick = () => report && download(`kubesage-${runId}.yaml`, manifestEntries().map(([f, t]) => `# ${f}\n${t.trim()}`).join("\n---\n") + "\n", "application/yaml");
$("#workload-search").addEventListener("input", renderWorkloads);
$("#status-filter").addEventListener("change", renderWorkloads);
$("#workloads").addEventListener("click", (ev) => {
  const button = ev.target.closest("[data-download]");
  if (!button) return;
  const w = report?.workloads.find((w) => w.id === button.dataset.download);
  if (w) download(`${appOf(w.id)}-${runId}.yaml`, Object.entries(w.manifests).map(([f, t]) => `# ${f}\n${t.trim()}`).join("\n---\n") + "\n", "application/yaml");
});
