"use strict";
// Under the hood: pipeline stages, the read-only shell, the journal and the wire inspector.

/* ---------- Architecture & CLI lab: everything here is read from the server, nothing is simulated ---------- */

const lab = {last: 0, events: [], filter: null, paused: false, status: null, trace: null, traceRun: null, kind: "llm",
  pick: 0, history: [], hpos: 0, recent: {}, timers: [], polling: false, pane: 0};
const STAGES = [[1, "Metrics"], [2, "Triage"], [3, "Remediation"], [4, "Autoscaling"], [5, "Nodes"]];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const hostOf = (url) => { try { return new URL(url).host; } catch (e) { return url || ""; } };
const pathOf = (url) => { try { return new URL(url).pathname; } catch (e) { return url || ""; } };
const ago = (s) => (s < 90 ? `${Math.max(0, Math.round(s))} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
const shq = (s) => `'${String(s).replace(/'/g, "'\\''")}'`;
const shortcuts = () => ["kubectl get pods -o wide", "kubectl get scaledobjects,hpa", "kubectl top nodes", "kubectl top pods",
  "kubectl events --types Warning",
  `promql 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="${config.namespace || "demo"}"}[5m]))'`,
  "kubesage route", "kubesage ledger", "kubesage plan", "kubesage runs", "help"];

function stageFacts(n) {
  const st = lab.status, r = report, c = cluster?.available ? cluster : null;
  const tier = (k) => st?.tiers?.[k] || null;
  const calls = (pred) => (r?.ledger || []).filter(pred);
  const total = (es, k) => es.reduce((a, e) => a + (e[k] || 0), 0);
  if (n === 1) {
    const vm = st?.vm, promql = (lab.trace?.exchanges || []).filter((x) => x.kind === "promql").length;
    return {comp: `VictoriaMetrics ${vm ? hostOf(vm.url) : ""}`, lines: [
      !vm ? "checking…" : vm.ok ? `${vm.series} live memory series in ${st.namespace}` : `unreachable: ${vm.error || ""}`,
      vm?.last_sample_age_s != null ? `newest sample ${ago(vm.last_sample_age_s)} old` : "",
      promql ? `${promql} PromQL queries in run ${lab.trace.run}` : "",
      r ? `${r.stats.length} workloads × ${r.window_days} days → stats, $0` : ""]};
  }
  if (n === 2) {
    const t = tier("triage"), e = calls((x) => x.stage === "triage")[0];
    return {comp: t ? `${t.label} · ${t.model}` : "…", lines: [
      e ? `${tok(e.input_tokens)} in / ${tok(e.output_tokens)} out · ${usd(e.cost_usd)} · ${e.latency_ms ?? "?"} ms${e.mock ? " · mock" : ""}` : "no run yet",
      r ? ["HEALTHY", "ROUTINE", "COMPLEX"].map((k) => `${r.workloads.filter((w) => w.triage.status === k).length} ${k.toLowerCase()}`).join(" · ") : "",
      st ? `via ${hostOf(st.provider.base_url)} (${st.mode})` : ""]};
  }
  if (n === 3) {
    const w = tier("worker"), s = tier("strategist"), es = calls((x) => x.stage !== "triage");
    const guards = (r?.workloads || []).reduce((a, x) => a + x.guardrail_notes.length, 0), np = r?.node_pool;
    return {comp: w && s ? `${w.label} + ${s.label} · ${w.model === s.model ? w.model : `${w.model}, ${s.model}`}` : "…", lines: [
      es.length ? `${es.length} calls · ${tok(total(es, "input_tokens"))} in (${tok(total(es, "cached_tokens"))} cached) · ${usd(total(es, "cost_usd"))}` : "",
      r ? `${guards} guardrail override${guards === 1 ? "" : "s"}` : "",
      np ? `pool ${np.before_nodes}× ${np.before_sku} → ${np.choice.nodes}× ${np.choice.sku}` : ""]};
  }
  if (n === 4) {
    const sc = Object.entries(c?.scaling || {}).filter(([, v]) => v.min != null || v.current != null), jobs = (c?.active_jobs || []).length;
    return {comp: `KEDA · ${sc.filter(([, v]) => v.min != null).length} ScaledObjects`, lines: [
      ...sc.slice(0, 3).map(([app, v]) => `${app}: ${v.current ?? "–"}→${v.desired ?? "–"} pods (${v.min ?? "?"}–${v.max ?? "?"})`
        + (v.cpu != null ? ` · CPU ${v.cpu}%${v.cpu_target ? ` of ${v.cpu_target}%` : ""}` : "")),
      !c ? "cluster unreachable" : jobs ? `traffic job running` : sc.length ? "" : "no autoscaling objects (before state)"]};
  }
  const nodes = (c?.nodes || []).filter((x) => x.role !== "pending");
  return {comp: `k3s · ${nodes.length} machine${nodes.length === 1 ? "" : "s"}`, lines: nodes.map((x) =>
    `${x.host || shortNode(x.name)} · ${cpuName(x.cpu_model) || "?"} · ${x.pods.filter(holding).length} pods`)};
}

function renderStages() {
  const root = $("#flow-stages");
  if (!root.children.length) {
    root.innerHTML = STAGES.map(([n, name]) => `<button class="stage" data-stage="${n}" aria-pressed="false">
      <span class="stage-led" aria-hidden="true"></span>
      <span class="stage-top"><span class="stage-n">STAGE 0${n}</span><span class="stage-name">${name}</span></span>
      <span class="stage-comp"></span><ul></ul></button>`).join("");
    root.addEventListener("click", (ev) => { const b = ev.target.closest(".stage"); if (b) setFilter(+b.dataset.stage); });
  }
  const now = Date.now(), live = (n) => now - (lab.recent[n] || 0) < 6000;
  STAGES.forEach(([n, name]) => {
    const card = root.querySelector(`[data-stage="${n}"]`), f = stageFacts(n), lines = f.lines.filter(Boolean);
    card.querySelector(".stage-comp").textContent = f.comp;
    card.querySelector("ul").replaceChildren(...lines.map((t) => Object.assign(document.createElement("li"), {textContent: t})));
    card.classList.toggle("active", live(n));
    card.classList.toggle("flowing", live(n) || live(n + 1));
    card.setAttribute("aria-pressed", String(lab.filter === n));
    card.setAttribute("aria-label", `Stage ${n}, ${name}: ${f.comp}. ${lines.join(". ")}. Filters the journal.`);
  });
}

function setFilter(n) {
  lab.filter = n && lab.filter !== n ? n : null;
  $("#jr-filter-label").textContent = lab.filter ? `stage ${lab.filter}: ${STAGES[lab.filter - 1][1].toLowerCase()}` : "all stages";
  $$("#jr-filters .tbtn").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.f === (lab.filter || 0))));
  renderJournal();
  renderStages();
}

function jrLine(e) {
  const d = new Date(e.at), div = document.createElement("div");
  div.className = e.level === "warning" ? "warn" : e.level === "error" || e.level === "critical" ? "error" : "";
  const stamp = `${MON[d.getMonth()]} ${String(d.getDate()).padStart(2, " ")} ${d.toTimeString().slice(0, 8)}`;
  const unit = e.unit.startsWith("k8s/") ? `${e.unit}:` : `kubesage[${lab.status?.pid ?? "?"}] ${e.unit}:`;
  [["t", `${stamp} ${lab.status?.host || ""} `], ["u", `${unit} `], ["st", e.stage ? `[${e.stage}] ` : ""], ["", e.msg]].forEach(([cls, text]) => {
    if (!text) return;
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text;
    div.append(span);
  });
  return div;
}

function renderJournal(fresh) {
  const box = $("#jr"), stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  const show = (e) => !lab.filter || e.stage === lab.filter;
  if (!fresh) box.replaceChildren(...lab.events.filter(show).slice(-600).map(jrLine));
  else {
    box.querySelector(".placeholder")?.remove();
    fresh.filter(show).forEach((e) => box.append(jrLine(e)));
  }
  while (box.children.length > 800) box.firstElementChild.remove();
  if (!box.children.length) box.append(Object.assign(document.createElement("div"), {className: "placeholder sh-dim",
    textContent: lab.filter ? `No stage ${lab.filter} events yet. Press an action above, or run a command.` : "Waiting for events…"}));
  if (stick && !lab.paused) box.scrollTop = box.scrollHeight;
}

async function pollEvents() {
  if (lab.polling) return;
  lab.polling = true;
  try {
    const res = await api(`/api/events?after=${lab.last}`);
    if (res.last < lab.last) { lab.last = 0; lab.events = []; renderJournal(); return; }  // the server restarted
    lab.last = res.last;
    const now = Date.now();
    res.events.forEach((e) => { if (e.stage && now - Date.parse(e.at) < 15000) lab.recent[e.stage] = now; });
    lab.events.push(...res.events);
    if (lab.events.length > 2000) lab.events.splice(0, lab.events.length - 2000);
    if (res.events.length || !$("#jr").children.length) renderJournal(res.events);
    renderStages();
  } catch (e) { /* the next poll retries; stage 1 shows the connection state */ }
  finally { lab.polling = false; }
}

async function refreshLab() {
  try { lab.status = await api("/api/lab"); } catch (e) { lab.status = null; }
  $("#sh-prompt").textContent = `kubesage@${lab.status?.host || "server"}:~$`;
  const run = lab.wirePicked || runId;
  if (run && (run !== lab.traceRun || lab.traceStale)) await loadTrace(run);
  else if (!run && !$("#wire-detail").childElementCount) renderWire();
  renderStages();
}

/* Shell */
function shPrint(text, cls = "") {
  const d = document.createElement("div");
  if (cls) d.className = cls;
  d.textContent = text;
  $("#sh-out").append(d);
}
const shScroll = () => ($("#sh-out").scrollTop = $("#sh-out").scrollHeight);

async function shRun(raw) {
  const cmd = raw.trim(), input = $("#sh-in");
  shPrint(`${$("#sh-prompt").textContent} ${cmd}`, "sh-prompt");
  if (cmd && lab.history.at(-1) !== cmd) lab.history.push(cmd);
  lab.hpos = lab.history.length;
  input.value = "";
  if (cmd === "clear") { $("#sh-out").replaceChildren(); return; }
  if (cmd === "history") { shPrint(lab.history.map((h, i) => `${String(i + 1).padStart(4)}  ${h}`).join("\n")); shScroll(); return; }
  if (!cmd) { shScroll(); return; }
  input.disabled = true;
  try {
    const res = await api("/api/shell", {cmd});
    if (res.out) shPrint(res.out, res.exit ? "sh-err" : "");
    shPrint(`[exit ${res.exit} · ${res.ms} ms]`, "sh-dim");
  } catch (e) { shPrint(e.message, "sh-err"); }
  finally { input.disabled = false; input.focus(); shScroll(); }
}

function shComplete(input) {
  const words = [...new Set([...shortcuts(), "clear", "history", "kubectl get ", "kubectl describe ", "kubectl logs ",
    "kubectl top ", "kubectl events ", "promql ", "kubesage "])];
  const options = words.filter((w) => w.startsWith(input.value) && w !== input.value);
  if (!options.length) return false;
  let common = options[0];
  options.forEach((o) => { while (!o.startsWith(common)) common = common.slice(0, -1); });
  if (common.length > input.value.length) { input.value = common; lab.tabbed = null; }
  else { shPrint(options.join("    "), "sh-dim"); shScroll(); lab.tabbed = input.value; }  // listed: the next Tab moves on
  return true;
}

/* Wire inspector */
const KINDS = [["promql", "PromQL"], ["llm", "LLM"], ["kubectl", "kubectl"], ["backfill", "backfill"]];

// After Analyze, Apply or a backfill the trace (and the run list) changed: reload it, keeping a run picked by hand.
function wireChanged() {
  lab.traceStale = true;
  if (!$("#lab").hidden) refreshLab();
}

async function loadTrace(run) {
  lab.traceRun = run;
  lab.traceStale = false;
  try { lab.trace = await api(`/api/trace?run=${encodeURIComponent(run)}`); }
  catch (e) { lab.trace = {run, error: e.message, exchanges: [], kubectl: [], backfill: null}; }
  lab.pick = 0;
  renderWire();
}

function wireItems() {
  const t = lab.trace || {exchanges: [], kubectl: []};
  if (lab.kind === "kubectl") return t.kubectl.map((k) => ({title: k.file, sub: k.cmd, k}));
  if (lab.kind === "backfill") return (t.backfill || []).map((x) => ({title: `${x.request.method} ${pathOf(x.request.url)}`,
    sub: `HTTP ${x.response.status} · ${x.latency_ms} ms`, x}));
  return (t.exchanges || []).filter((x) => x.kind === lab.kind).map((x) => (x.kind === "llm"
    ? {title: `${x.llm_stage} · ${x.workload}`, sub: `${x.label} · ${x.model}${x.sent ? ` · ${x.latency_ms} ms` : " · mock, not sent"}${x.attempt > 1 ? ` · retry ${x.attempt}` : ""}`, x}
    : {title: x.request.params?.query || pathOf(x.request.url),
       sub: `${pathOf(x.request.url).split("/").pop()} · ${x.response?.series ?? "?"} series · ${x.latency_ms ?? "?"} ms`, x}));
}

function renderWire() {
  const t = lab.trace || {exchanges: [], kubectl: []}, sel = $("#wire-run");
  const runs = reports.length ? reports : runId ? [{run: runId, mode: report?.mode}] : [];
  sel.replaceChildren(...runs.map((r) => Object.assign(document.createElement("option"), {value: r.run, textContent: `${r.run} · ${r.mode}`})));
  sel.closest("label").hidden = !runs.length;
  if (lab.traceRun) sel.value = lab.traceRun;
  const count = {promql: t.exchanges.filter((x) => x.kind === "promql").length, llm: t.exchanges.filter((x) => x.kind === "llm").length,
    kubectl: t.kubectl.length, backfill: (t.backfill || []).length};
  $("#wire-kinds").innerHTML = KINDS.map(([k, label]) => `<button class="tbtn" role="tab" data-kind="${k}" aria-selected="${lab.kind === k}"
    aria-pressed="${lab.kind === k}">${label} ${count[k]}</button>`).join(" ");
  const items = wireItems();
  lab.pick = Math.min(lab.pick, Math.max(items.length - 1, 0));
  $("#wire-list").replaceChildren(...items.map((it, i) => {
    const b = document.createElement("button");
    b.setAttribute("role", "option");
    b.setAttribute("aria-selected", String(i === lab.pick));
    b.dataset.i = i;
    b.append(document.createTextNode(it.title), Object.assign(document.createElement("span"), {textContent: it.sub}));
    return b;
  }));
  renderWireDetail(items[lab.pick]);
}

function addBlock(box, tag, text, cls) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (cls) el.className = cls;
  box.append(el);
  return el;
}

function addJson(box, value) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2), limit = 12000;
  const pre = addBlock(box, "pre", text.length > limit ? text.slice(0, limit) + "\n…" : text);
  if (text.length > limit) {
    const more = addBlock(box, "button", `show all ${(text.length / 1000).toFixed(0)}k characters`, "tbtn");
    more.onclick = () => { pre.textContent = text; more.remove(); };
  }
}

function curlFor(x) {
  const req = x.request;
  if (x.kind === "llm") {
    const env = (req.headers?.Authorization || "").replace(/^Bearer /, "") || "$API_KEY";
    return `curl -sS ${shq(req.url)} \\\n  -H "Authorization: Bearer ${env}" \\\n  -H 'Content-Type: application/json' \\\n  --data-binary @- <<'JSON'\n${JSON.stringify(req.body)}\nJSON`;
  }
  const params = Object.entries(req.params || {}).map(([k, v]) => `--data-urlencode ${shq(`${k}=${v}`)}`).join(" ");
  if (req.method === "GET") return `curl -sG ${shq(req.url)}${params ? " " + params : ""}`;
  if (req.body_bytes) return `curl -sS -X POST ${shq(req.url)} -H 'Content-Encoding: gzip' --data-binary @series.jsonl.gz`;
  return `curl -sS -X POST -G ${shq(req.url)}${params ? " " + params : ""}`;
}

async function copyText(text, button) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {  // plain http on the LAN has no async clipboard
    const area = Object.assign(document.createElement("textarea"), {value: text});
    document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
  }
  const label = button.textContent;
  button.textContent = "copied";
  setTimeout(() => (button.textContent = label), 1200);
}

function renderWireDetail(item) {
  const box = $("#wire-detail");
  box.replaceChildren();
  if (!item) {
    addBlock(box, "p", lab.trace?.error ? lab.trace.error
      : lab.kind === "backfill" ? "No backfill in this server session yet. Backfill 30 days records its four VictoriaMetrics requests here."
      : !lab.trace ? "No analysis has run yet. Run analysis records every request each stage sends and what came back."
      : (lab.trace?.exchanges || []).length ? "Nothing of this kind in this run."
      : "This run predates request tracing. Run an analysis to record what each stage sends.", "sh-dim");
    return;
  }
  if (item.k) {
    addBlock(box, "div", `$ ${item.k.cmd}`, "req");
    addBlock(box, "button", "copy command", "tbtn").onclick = (ev) => copyText(item.k.cmd, ev.currentTarget);
    addBlock(box, "h4", `manifest · ${item.k.file}`);
    addJson(box, item.k.manifest);
    return;
  }
  const x = item.x, req = x.request;
  const query = req.params ? "?" + new URLSearchParams(Object.entries(req.params).map(([k, v]) => [k, String(v)])).toString() : "";
  addBlock(box, "div", `${req.method} ${req.url}${query}`, "req");
  addBlock(box, "button", "copy as curl", "tbtn").onclick = (ev) => copyText(curlFor(x), ev.currentTarget);
  if (req.headers) { addBlock(box, "h4", "request headers"); addJson(box, Object.entries(req.headers).map(([k, v]) => `${k}: ${v}`).join("\n")); }
  if (req.body) { addBlock(box, "h4", "request body, as sent"); addJson(box, req.body); }
  if (req.body_preview) {
    addBlock(box, "h4", `request body: ${req.body_bytes.series} JSON lines, ${(req.body_bytes.raw / 1e6).toFixed(1)} MB raw, ${(req.body_bytes.gzip / 1e6).toFixed(1)} MB gzip; first two shown, arrays cut to 3 points`);
    addJson(box, req.body_preview);
  }
  addBlock(box, "h4", `response${x.latency_ms != null ? ` · ${x.latency_ms} ms` : ""}`);
  const res = x.response || {};
  if (res.note) addBlock(box, "p", res.note, "sh-dim");
  if (x.kind === "llm" && res.body?.choices) {
    addJson(box, {status: res.status, id: res.body.id, model: res.body.model, usage: res.body.usage});
    addBlock(box, "h4", "message content");
    const content = res.body.choices[0]?.message?.content || "";
    try { addJson(box, JSON.parse(content)); } catch (e) { addJson(box, content); }
  } else addJson(box, res.body ?? res);
}



/* Panes and view switching */
function selectPane(i) {
  lab.pane = i;
  [0, 1, 2].forEach((p) => $(`#pane-${p}`).classList.toggle("on", p === i));
  $$("#tmux-windows button").forEach((b) => b.setAttribute("aria-selected", String(+b.dataset.pane === i)));
}

function tick() {
  const d = new Date();
  $("#tmux-clock").textContent = `"${lab.status?.host || ""}" ${d.toTimeString().slice(0, 5)} ${String(d.getDate()).padStart(2, "0")}-${MON[d.getMonth()]}-${String(d.getFullYear()).slice(2)}`;
  renderStages();
}

function startLab() {
  if (lab.timers.length) return;
  refreshLab().then(pollEvents);
  lab.timers = [setInterval(pollEvents, 1000), setInterval(refreshLab, 5000), setInterval(tick, 1000)];
  tick();
}
function stopLab() { lab.timers.forEach(clearInterval); lab.timers = []; }

function initLab() {
  $("#xkeys").innerHTML = shortcuts().map((c) => `<button class="tbtn" data-cmd="${esc(c)}">${esc(c.length > 34 ? c.slice(0, 32) + "…" : c)}</button>`).join("")
    + `<button class="tbtn" data-cmd="clear">clear</button>`;
  $("#xkeys").onclick = (ev) => { const b = ev.target.closest("[data-cmd]"); if (b) shRun(b.dataset.cmd); };
  $("#sh-form").onsubmit = (ev) => { ev.preventDefault(); shRun($("#sh-in").value); };
  $("#sh-clear").onclick = () => $("#sh-out").replaceChildren();
  $("#sh-in").addEventListener("keydown", (ev) => {
    const input = ev.currentTarget;
    if (ev.key === "ArrowUp" && lab.hpos > 0) { ev.preventDefault(); input.value = lab.history[--lab.hpos]; }
    else if (ev.key === "ArrowDown") { ev.preventDefault(); lab.hpos = Math.min(lab.hpos + 1, lab.history.length); input.value = lab.history[lab.hpos] || ""; }
    // Tab completes; when there is nothing (more) to complete it moves focus as usual, and Shift+Tab always does.
    else if (ev.key === "Tab" && !ev.shiftKey && input.value && lab.tabbed !== input.value && shComplete(input)) ev.preventDefault();
    else if (ev.key === "l" && ev.ctrlKey) { ev.preventDefault(); $("#sh-out").replaceChildren(); }
  });
  $("#jr-filters").innerHTML = [0, 1, 2, 3, 4, 5].map((f) => `<button class="tbtn" data-f="${f}" aria-pressed="${f === 0}"
    aria-label="${f ? `Stage ${f}` : "All stages"}">${f || "all"}</button>`).join(" ");
  $("#jr-filters").onclick = (ev) => { const b = ev.target.closest("[data-f]"); if (b) setFilter(+b.dataset.f); };
  $("#jr-pause").onclick = (ev) => {
    lab.paused = !lab.paused;
    ev.currentTarget.setAttribute("aria-pressed", String(lab.paused));
    ev.currentTarget.textContent = lab.paused ? "resume" : "pause";
    if (!lab.paused) $("#jr").scrollTop = $("#jr").scrollHeight;
  };
  $("#wire-run").onchange = (ev) => {
    lab.wirePicked = ev.currentTarget.value === runId ? null : ev.currentTarget.value;  // the latest: keep following it
    loadTrace(ev.currentTarget.value);
  };
  $("#wire-kinds").onclick = (ev) => { const b = ev.target.closest("[data-kind]"); if (b) { lab.kind = b.dataset.kind; lab.pick = 0; renderWire(); } };
  $("#wire-list").onclick = (ev) => { const b = ev.target.closest("[data-i]"); if (b) { lab.pick = +b.dataset.i; renderWire(); } };
  $("#wire-list").addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    ev.preventDefault();
    lab.pick = Math.max(0, Math.min(wireItems().length - 1, lab.pick + (ev.key === "ArrowDown" ? 1 : -1)));
    renderWire();
    $(`#wire-list [data-i="${lab.pick}"]`)?.focus();
  });
  $("#tmux-windows").innerHTML = ["shell", "journal", "wire"].map((name, i) =>
    `<button role="tab" data-pane="${i}" aria-selected="${i === 0}" aria-controls="pane-${i}">${i}:${name}</button>`).join("");
  $("#tmux-windows").onclick = (ev) => { const b = ev.target.closest("[data-pane]"); if (b) selectPane(+b.dataset.pane); };
  $("#lab-analyze").onclick = () => $("#btn-analyze").click();
  $("#lab-apply").onclick = () => $("#btn-apply").click();
  $("#lab-spike").onclick = () => $("#btn-spike").click();
  $(".lab-jump").onclick = (ev) => {
    const b = ev.target.closest("[data-jump]");
    if (b) $("#" + b.dataset.jump).scrollIntoView({behavior: reduceMotion.matches ? "auto" : "smooth", block: "start"});
  };
  $("#lab-backfill").onclick = (ev) => {
    if (!confirm("Backfill replaces ALL metric history for this namespace in VictoriaMetrics: the 30-day scenario history and "
      + "everything recorded live since. The next analysis then reads only the regenerated history. Continue?")) return;
    busy(ev.currentTarget, async () => {
      const res = await api("/api/backfill", {days: 30});
      log(`backfill: ${res.samples.toLocaleString()} samples, ${res.series} series, ${res.days} days in ${res.seconds} s`, "ok");
      lab.kind = "backfill";
      wireChanged();
    });
  };
  shPrint("KubeSage lab shell. Read-only: kubectl get/describe/top/logs/events, promql, kubesage route|ledger|plan|runs. Type help.", "sh-dim wrap");
  selectPane(0);
  renderStages();
  renderJournal();
}
