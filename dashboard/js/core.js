"use strict";
// Shared helpers and state: $, esc, money formatting, icons, the API client, busy state and the activity log.

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const usd = (v) => (Math.abs(v) < 1 ? "$" + v.toFixed(4) : "$" + v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}));
const tok = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
const cores = (m) => (m == null ? "—" : (m / 1000).toFixed(2));
const mib = (v) => (v == null ? "—" : v >= 1024 ? (v / 1024).toFixed(1) + " GiB" : Math.round(v) + " MiB");
const sum = (xs, key) => xs.reduce((a, x) => a + (x[key] ?? 0), 0);
// A measured total only when every pod has a sample (a Pending pod uses nothing): a partial sum would read as spare
// capacity. coverage() says how many were sampled when some weren't.
const sampled = (x, key) => x[key] != null || x.phase === "Pending";
const measured = (xs, key) => xs.every((x) => sampled(x, key)) ? sum(xs, key) : null;
const coverage = (xs, key) => { const n = xs.filter((x) => sampled(x, key)).length; return n < xs.length ? `${n} of ${xs.length} pods sampled` : ""; };
const partial = (xs, key) => coverage(xs, key) ? ` <span class="muted">(${coverage(xs, key)})</span>` : "";
const shortNode = (n) => n.replace(/^k3d-[^-]+-/, "");
const shortPod = (p, app) => (p.startsWith(app + "-") ? "…" + p.slice(app.length) : p);
const appOf = (id) => id.split("/").pop();
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

const ICON = {
  spark: '<path d="M12 3.5 13.9 9l5.6 1.9-5.6 1.9L12 18.4l-1.9-5.6L4.5 10.9 10.1 9z"/><path d="M19 3v4M17 5h4"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/>',
  pr: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M6 8.5V21M13 6h3a2 2 0 0 1 2 2v7.5"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  chev: '<path d="m6 9 6 6 6-6"/>',
  up: '<path d="m18 15-6-6-6 6"/>',
  next: '<path d="m9 6 6 6-6 6"/>',
  prev: '<path d="m15 6-6 6 6 6"/>',
  play: '<path d="M7 4.5v15l12-7.5z"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  side: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M14 4v16M17 8h1M17 11h1"/>',
  loader: '<path d="M21 12a9 9 0 1 1-6.2-8.6"/>',
  healthy: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.7 2.7L16 9.5"/>',
  routine: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  complex: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
};
const icon = (name, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;
const STATUS = {
  HEALTHY: {label: "Healthy", icon: "healthy", color: "var(--good)"},
  ROUTINE: {label: "Routine", icon: "routine", color: "var(--warn)"},
  COMPLEX: {label: "Complex", icon: "complex", color: "var(--crit)"},
};
const statusBadge = (st) => `<span class="badge"><span style="color:${STATUS[st].color};display:inline-flex">${icon(STATUS[st].icon)}</span>${STATUS[st].label}</span>`;

let TIER = {triage: "triage", worker: "worker", strategist: "strategist"};  // replaced by the report's labels
let config = {}, report = null, runId = null, cluster = null, sel = null, hoverApp = null, lastSig = "";
let applied = null, spikeEnds = 0, spikeJob = null, isBusy = false, polling = false, pollAgain = false, reports = [], latestRun = null;
const order = [];  // workload -> colour slot, first seen first; the report goes first so colours never shuffle
const seenPods = new Set();

function color(app) {
  let i = order.indexOf(app);
  if (i < 0) i = order.push(app) - 1;
  return i < 8 ? `var(--s${i + 1})` : "var(--muted)";  // a 9th workload gets neutral grey, never a generated hue
}

async function api(path, body) {
  const opts = body === undefined ? {headers: {"X-KubeSage": "1"}}
    : {method: "POST", headers: {"Content-Type": "application/json", "X-KubeSage": "1"}, body: JSON.stringify(body)};
  // Analyze is bounded by the server's own LLM timeouts and holds the operation lock until it ends; giving up
  // here would report a failure while the run is still going and make every retry a 409.
  if (path !== "/api/analyze") opts.signal = AbortSignal.timeout(body === undefined ? 45000 : 360000);
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({error: res.statusText}));
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), {status: res.status, data});
  return data;
}

// Polling updates the numbers without taking keyboard focus away from an inspector or diagram.
function replaceHTML(el, html) {
  const active = document.activeElement, within = active && el.contains(active);
  const attr = within && ["data-pod", "data-node", "data-pick", "data-close", "data-app", "data-agent", "data-exec", "data-goto"]
    .find((a) => active.hasAttribute(a));
  const selector = attr ? `${active.tagName.toLowerCase()}[${attr}="${CSS.escape(active.getAttribute(attr))}"]${active.hasAttribute("data-key") ? `[data-key="${CSS.escape(active.getAttribute("data-key"))}"]` : ""}` : null;
  const open = [...el.querySelectorAll("details[data-preserve][open]")].map((d) => d.dataset.preserve);
  el.innerHTML = html;
  open.forEach((id) => { const d = el.querySelector(`details[data-preserve="${CSS.escape(id)}"]`); if (d) d.open = true; });
  // Back to the same control; failing that, to this view (never to a card in another view).
  if (within) (selector && el.querySelector(selector) || el.closest("[data-panel]") || $("#cluster-card")).focus({preventScroll: true});
}

// An inline message inside a view that isn't open is no message at all: say it in the toast as well.
const inOpenView = (el) => !!el && !el.closest("[data-panel]")?.hidden;
let toastTimer = null;
function toast(message, kind = "ok") {
  const el = $("#toast");
  el.hidden = false;
  el.dataset.kind = kind;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  $("#toast-text").textContent = message;
  clearTimeout(toastTimer);
  if (kind !== "err") toastTimer = setTimeout(() => (el.hidden = true), 8000);  // errors stay until dismissed
}

function notice(message, kind = "ok") {
  const el = $("#notice");
  el.hidden = false;
  el.dataset.kind = kind;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  $("#notice-text").textContent = message;
  if (!inOpenView(el)) toast(message, kind);
}

function updateControls() {
  const blocked = isBusy || !!cluster?.operation;
  // A cluster whose pods and workloads couldn't be read is reachable but unknown: nothing that changes it runs blind.
  const live = cluster?.available && cluster.objects !== false;
  $$("[data-act]").forEach((b) => (b.disabled = blocked));
  $("#btn-apply").disabled = blocked || !report || !live;
  $("#btn-gitops").disabled = blocked || !report;
  $("#btn-reset").disabled = blocked || !live || config.namespace !== "demo";
  const active = (cluster?.active_jobs || []).length > 0 || spikeEnds > Date.now();
  $("#btn-spike").disabled = blocked || !live || active;
  $("#report-select").disabled = isBusy || !reports.length;
  $("#dryrun").disabled = isBusy;
  ["#btn-export", "#btn-manifests"].forEach((s) => ($(s).disabled = !report));
  ["analyze", "apply", "spike"].forEach((k) => ($(`#lab-${k}`).disabled = $(`#btn-${k}`).disabled));
  $("#lab-backfill").disabled = blocked || config.namespace !== "demo";
}

function log(msg, kind = "") {
  const li = document.createElement("li");
  li.className = kind;
  const t = document.createElement("time");
  t.textContent = new Date().toLocaleTimeString();
  li.append(t, document.createTextNode(msg));
  $("#log").prepend(li);
}

async function busy(button, fn) {
  if (isBusy) return;
  const ic = button.querySelector(".ic"), original = ic && ic.outerHTML;
  isBusy = true; updateControls();
  document.body.classList.add("busy");
  $("main").setAttribute("aria-busy", "true");
  if (ic) ic.outerHTML = icon("loader", "spin");
  try { await fn(); } catch (e) {
    log(e.message, "err"); notice(e.message, "err");
    (e.data?.steps || []).filter((s) => !s.ok).forEach((s) => log(`${s.file}: ${s.output}`, "err"));
    if (e.data?.steps) { applied = null; await refreshCluster(); }
  }
  finally {
    const cur = button.querySelector(".ic");
    if (cur && original) cur.outerHTML = original;
    isBusy = false; updateControls();
    document.body.classList.remove("busy");
    $("main").setAttribute("aria-busy", "false");
  }
}
