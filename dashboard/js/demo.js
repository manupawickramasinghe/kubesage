"use strict";
// Demo: the run of show for presenting KubeSage, and the caption bar that walks the presenter through the real views.
// Each step names the view it happens on, what to point at, what the audience is seeing and the one button that does it.

const demo = {on: false, step: 0, min: false};
try { Object.assign(demo, JSON.parse(sessionStorage.getItem("kubesage-demo") || "{}")); } catch (e) { /* storage blocked */ }
const saveDemo = () => {
  try { sessionStorage.setItem("kubesage-demo", JSON.stringify({on: demo.on, step: demo.step, min: demo.min})); } catch (e) { /* storage blocked */ }
};

const ACTS = ["Save money", "Fix a live incident", "Wrap up"];
const VIEW_NAMES = {demo: "Demo", dashboard: "FinOps", agents: "Agent team", lab: "Under the hood", tmux: "tmux"};
const tcName = () => $("#tc-select").value;
const activeTcs = () => Object.keys(team.tcs?.active || {});
const hhmm = (t) => new Date(t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});

function clusterUse() {
  if (!cluster?.available) return null;
  const nodes = cluster.nodes.filter((n) => n.role !== "pending");
  const pods = nodes.flatMap((n) => n.pods).filter((p) => !["Terminating", "Succeeded", "Failed"].includes(p.phase));
  const req = sum(pods, "cpu_request_m"), used = measured(pods, "cpu_used_m");
  return {nodes, pods, ready: nodes.filter((n) => n.ready).length, podsReady: pods.filter((p) => p.ready).length,
    pct: cluster.metrics && req && used != null ? Math.round((used / req) * 100) : null};
}
const savings = () => (report?.node_pool ? report.node_pool.before_monthly_usd - report.node_pool.after_monthly_usd : null);
const runSecs = (run) => {  // null while a run has no finish time
  const s = (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000;
  return Number.isFinite(s) ? Math.round(s) : null;
};
// FinOps steps press the dashboard's own buttons, so busy state, notices and the activity log behave as usual.
const proxy = (sel) => ({run: () => $(sel).click(), enabled: () => !$(sel).disabled, busy: () => !!$(sel + " .spin")});

// status() returns [state, text]; state is done, live (happening now), todo or warn.
const DEMO = [
  {act: 0, title: "A real cluster", view: "dashboard", target: "#cluster-card",
    say: "This is not a simulation. A desktop runs the Kubernetes control plane and a laptop joined as a worker. Each glass "
      + "is the CPU a pod reserves and the block inside is what it really uses. Most glasses are nearly empty, and the bill pays for the glass.",
    status() {
      const u = clusterUse();
      if (!cluster) return ["todo", "Connecting to the cluster…"];
      if (!u) return ["warn", "Cluster not reachable"];
      return [u.ready === u.nodes.length ? "done" : "warn", `${u.ready}/${u.nodes.length} machines · ${u.podsReady}/${u.pods.length} pods ready`
        + (u.pct != null ? ` · ${u.pct}% of reserved CPU in use` : "")];
    }},
  {act: 0, title: "Find the waste", view: "dashboard", target: "#figures",
    say: "KubeSage reads 30 days of usage. Statistics are free, so every workload gets one cheap triage call and only the "
      + "hard ones go to a bigger model. The whole analysis costs about a cent.",
    actions: [{label: "Analyze", icon: "spark", ...proxy("#btn-analyze")}],
    status() {
      if ($("#btn-analyze .spin")) return ["live", "Analyzing: free statistics, one triage call, then a bigger model where needed…"];
      if (!report) return ["todo", "Not run yet"];
      const s = savings();
      return ["done", `${s != null && s > 0 ? `${usd(s)}/mo found · ` : ""}${report.workloads.filter((w) => w.recommendation).length} of `
        + `${report.workloads.length} workloads change · analysis cost ${usd(report.llm_cost_usd)}`];
    }},
  {act: 0, title: "How it decided", view: "dashboard", target: "#decision-card",
    say: "Each line is a workload. Healthy ones stop after free statistics, routine ones go to a medium model and complex ones "
      + "to a large one. Guardrails then check every number against what was measured before anything can be applied.",
    status() {
      if (!report) return ["todo", "Analyze first"];
      const clamps = report.workloads.reduce((n, w) => n + (w.guardrail_notes || []).length, 0);
      return ["done", `${plural(report.ledger.length, "model call")} · ${usd(report.llm_cost_usd)} · ${plural(clamps, "guardrail correction")}`];
    }},
  {act: 0, title: "Apply the plan", view: "dashboard", target: "#cluster-card",
    say: "One click writes the plan to the cluster: right-sized requests, a KEDA scaling rule and a cheaper machine type. "
      + "The pods restart with smaller glasses.",
    actions: [{label: "Apply fixes", icon: "check", ...proxy("#btn-apply")}],
    status() {
      const plan = cluster?.plan?.run === runId ? cluster.plan : null;
      if ($("#btn-apply .spin")) return ["live", "Writing the plan to the cluster…"];
      if (!report) return ["todo", "Analyze first"];
      if (plan?.state === "applied") return ["done", `Live specs match the plan${plan.ready ? " · workloads ready" : " · rolling out"}`];
      if (plan?.state === "partial") return ["live", `${plan.matched} of ${plan.total} workloads match the plan`];
      return ["todo", "Not applied: the cluster still has its original, oversized requests"];
    }},
  {act: 0, title: "Prove it still scales", view: "dashboard", target: "#cluster-card",
    say: "Smaller is only safe if it still copes with a spike. This sends real HTTP requests to checkout-web: CPU rises and "
      + "KEDA adds replicas.",
    actions: [{label: "Send traffic", icon: "zap", ...proxy("#btn-spike")}],
    status() {
      const t = cluster?.traffic, sc = cluster?.scaling?.["checkout-web"];
      const reps = sc?.max != null ? ` · ${sc.current ?? "–"}/${sc.max} replicas · CPU ${sc.cpu ?? "–"}%` : "";
      if ($("#btn-spike .spin")) return ["live", "Starting the traffic job…"];
      if (t?.status === "running" || t?.status === "pending") return ["live", `Traffic running${reps}`];
      if (t?.status === "failed") return ["warn", "The traffic job failed"];
      if (t?.status === "completed") return ["done", `${(t.succeeded_requests ?? 0).toLocaleString()} requests answered`
        + (t.p95_ms != null ? ` · p95 ${t.p95_ms.toLocaleString()} ms` : "") + reps];
      return ["todo", sc?.max != null ? `Ready${reps}` : "Apply the plan first, so KEDA can scale"];
    }},
  {act: 1, title: "Break something", view: "agents", target: "#team .tc-box", tc: true,
    say: () => `${(team.tcs?.catalog || []).find((t) => t.name === tcName())?.summary || "Pick a test case."} `
      + "It runs on the laptop, and Alertmanager fires an alert within about a minute.",
    actions: [{label: "Start test", icon: "zap", run: () => tcAction("start", tcName()), enabled: () => !$("#tc-start").disabled}],
    status() {
      const active = team.tcs?.active || {}, alerts = team.tcs?.alerts || [], names = Object.keys(active);
      if (!team.tcs) return ["todo", "Loading test cases…"];
      if (!names.length) return team.tcs.cluster === false ? ["warn", "Cluster unreachable"] : ["todo", "Nothing running"];
      const restarts = active[names[0]].restarts;
      return [alerts.length ? "done" : "live", `${names.join(", ")} running${restarts ? ` · ${plural(restarts, "restart")}` : ""}`
        + (alerts.length ? ` · alert ${alerts[0].name} firing` : " · waiting for an alert")];
    }},
  {act: 1, title: "The team investigates", view: "agents", target: "#team-flow",
    say: "Four small models read metrics, the Kubernetes API, Alertmanager and security posture at the same time. A finding "
      + "must cite a signal it collected or it is dropped. The Thinker, a large model, reads the findings next to the "
      + "workload's own source code and decides the root cause.",
    actions: [{label: "Run agent team", icon: "spark", run: teamRun, enabled: () => !$("#team-run").disabled}],
    status() {
      const run = team.data?.run, pending = team.data?.pending;
      if (pending) return ["live", `Starts in ${pending.in_s} s, after alert ${pending.alert}`];
      if (!run) return ["todo", team.data?.auto ? "Starts by itself 45 s after an alert" : "Not run yet"];
      if (run.state === "running") {
        const now = Object.values(run.agents || {}).filter((a) => a.state === "running").map((a) => a.title);
        return ["live", `Running: ${now.join(", ") || "starting"}…`];
      }
      if (run.error || run.state === "failed") return ["warn", `Failed: ${run.error || "see the tmux view"}`];
      const secs = runSecs(run);
      return ["done", `${plural(run.diagnosis?.issues.length ?? 0, "issue")} diagnosed${secs != null ? ` in ${secs} s` : ""} for `
        + `${usd(run.cost_usd || 0)}${run.started_at ? ` · ${hhmm(run.started_at)}` : ""}`];
    }},
  {act: 1, title: "Ship the fix", view: "agents", target: "#team-detail",
    say: "The Executor, a medium model, writes the fix as a git commit. Before it is offered, every edit must apply exactly, "
      + "the YAML must build and changed Python must compile. A person reads the diff and decides whether to apply it or open a pull request.",
    actions: [{label: "Apply to cluster", icon: "check", run: () => execAction("apply"),
      enabled: () => !!team.data?.run?.executor?.commit && !team.busy}],
    status() {
      const run = team.data?.run, ex = run?.executor;
      if (!ex?.commit) return ["todo", run?.state === "running" ? "Waiting for the diagnosis…" : "No change yet"];
      if (ex.applied) return [ex.applied.ok ? "done" : "warn", `${ex.applied.ok ? "Applied" : "Applied with errors"} at `
        + `${hhmm(ex.applied.at * 1000)} · commit ${ex.commit.slice(0, 8)}`];
      return ["live", `Commit ${ex.commit.slice(0, 8)} · ${plural(ex.edits?.length ?? 0, "file")} · ${ex.pr_url ? "pull request open" : "ready to apply"}`];
    }},
  {act: 2, title: "Why it matters", view: "demo", target: "#demo-why",
    say: "Small models do the legwork, so an incident costs cents, not dollars. Nothing is trusted without evidence, and every "
      + "change is a git commit a person approves. Stopping the test case puts the laptop back.",
    actions: [{label: "Stop test case", icon: "x", run: async () => { for (const n of activeTcs()) await tcAction("stop", n); },
      enabled: () => activeTcs().length > 0 && !team.busy}],
    status() {
      const n = activeTcs().length;
      return n ? ["live", `${plural(n, "test case")} still running`] : ["done", "The lab is quiet"];
    }},
];
const sayOf = (s) => (typeof s.say === "function" ? s.say() : s.say);
// Rewrites only on a change: these refresh every second, and a rebuilt button loses a click in progress.
const setHTML = (el, html) => { if (el.dataset.html !== html) { replaceHTML(el, html); el.dataset.html = html; } };
const dotOf = (state) => `<span class="${state === "live" ? "pulse-dot" : "dot"}" style="--c:var(--${{done: "good", live: "rec", warn: "warn"}[state] || "ring"})"></span>`;

function stepButtons(s) {
  const tc = s.tc ? `<label class="sr-only" for="dk-tc">Test case</label><select id="dk-tc" data-tc></select>` : "";
  return tc + (s.actions || []).map((a, k) => `<button class="btn btn-primary btn-sm" data-run="${k}">${icon(a.icon)}<span>${esc(a.label)}</span></button>`).join("");
}

// Copies of the test-case picker follow the real one in the agent team view, which owns the selection.
function syncPickers(root) {
  root.querySelectorAll("[data-tc]").forEach((sel) => {
    const html = $("#tc-select").innerHTML;
    if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
    if (document.activeElement !== sel) sel.value = tcName();
  });
}

/* The run of show on the Demo view */
function buildDemo() {
  const acts = ACTS.map((name, a) => {
    const steps = DEMO.map((s, i) => [s, i]).filter(([s]) => s.act === a);
    const minutes = {0: "about 3 min", 1: "about 3 min", 2: "a minute"}[a];
    return `<div class="act-col"><h2><span>Act ${a + 1}</span>${esc(name)}<small>${minutes}</small></h2><ol class="ds-list">${steps.map(([s, i]) => `
      <li class="ds" data-i="${i}"><span class="ds-n">${i + 1}</span>
        <div class="ds-b"><div class="ds-h"><b>${esc(s.title)}</b><span class="ds-where">${esc(VIEW_NAMES[s.view])}</span></div>
          <p class="ds-say"></p><p class="ds-st"></p></div>
        <button class="btn btn-ghost btn-sm" data-go="${i}">Present ${icon("next")}</button></li>`).join("")}</ol></div>`;
  });
  $("#demo-acts").innerHTML = acts.join("");
  $("#demo-start").innerHTML = `${icon("play")}<span>Start the demo</span>`;
}

function renderDemo() {
  if ($("#demo").hidden) return;
  $$("#demo-acts .ds").forEach((li) => {
    const s = DEMO[+li.dataset.i], [state, text] = s.status(), say = sayOf(s);
    setHTML(li.querySelector(".ds-st"), `${dotOf(state)}${esc(text)}`);
    if (li.querySelector(".ds-say").textContent !== say) li.querySelector(".ds-say").textContent = say;
    li.dataset.state = state;
    li.classList.toggle("now", demo.on && +li.dataset.i === demo.step);
  });
  $("#demo-start").lastChild.textContent = demo.on ? `Resume at step ${demo.step + 1}` : "Start the demo";
  setHTML($("#demo-figs"), demoFigures());
  setHTML($("#demo-preflight"), preflight());
}

function demoFigures() {
  const s = savings(), run = team.data?.run, agents = team.data?.team?.agents || [], u = clusterUse();
  const waste = report && s != null ? fig("Waste found", `${usd(Math.max(0, s))}<small> /mo</small>`,
    `potential node-pool savings, found for ${usd(report.llm_cost_usd)}`) : fig("Waste found", "—", "Act 1 finds it");
  let incident = fig("Alert to fix", "—", "Act 2 measures it");
  if (run?.state === "running") incident = fig("Alert to fix", "running…", `run ${esc(run.id)}`);
  else if (run && !run.error && run.state === "done") incident = fig("Alert to fix", runSecs(run) != null ? `${runSecs(run)}<small> s</small>` : "done",
    `${plural(run.diagnosis?.issues.length ?? 0, "issue")} and a ${run.executor?.commit ? "commit" : "diagnosis"} for ${usd(run.cost_usd || 0)}`);
  const cpus = [...new Set((u?.nodes || []).map((n) => cpuName(n.cpu_model)).filter(Boolean))];
  const hw = u ? fig("Real hardware", `${u.nodes.length}<small> machines</small>`, `${esc(cpus.join(" + "))} · ${sum(u.nodes, "cpu_cores")} threads`)
    : fig("Real hardware", "—", "connecting…");
  const by = (role) => agents.filter((a) => a.role === role);
  const team6 = agents.length ? fig("Agent team", `${agents.length}<small> agents</small>`,
    `${by("subagent").length} sub-agents on ${esc(by("subagent")[0]?.label || "—")} · Thinker and Executor on `
    + `${esc([...new Set([...by("thinker"), ...by("executor")].map((a) => a.model))].join(", "))}`) : fig("Agent team", "—", "loading…");
  return waste + incident + hw + team6;
}

function preflight() {
  const chip = (state, text, button = "") => `<span class="pf" data-state="${state}">${icon(state === "ok" ? "healthy" : state === "bad" ? "complex" : "routine")}${text}${button}</span>`;
  const u = clusterUse(), plan = cluster?.plan, active = activeTcs();
  const items = [
    !config.mode ? chip("wait", "Server…") : config.mode === "mock" ? chip("bad", "Mock LLM: no API key, answers are rule-based")
      : chip("ok", "Live LLM"),
    !cluster ? chip("wait", "Cluster…") : u && u.ready === u.nodes.length ? chip("ok", `${u.ready}/${u.nodes.length} machines ready`)
      : chip("bad", "Cluster not ready"),
    plan?.state === "applied" || plan?.state === "partial"
      ? chip("bad", "FinOps plan already applied", `<button class="linkish" data-pf="reset">Reset demo</button>`)
      : chip("ok", "FinOps demo in its oversized ‘before’ state"),
    active.length ? chip("bad", `${esc(active.join(", "))} running`, `<button class="linkish" data-pf="stop">Stop</button>`)
      : team.tcs ? chip("ok", "No test case running") : chip("wait", "Test cases…"),
    team.data ? chip("info", `Auto-run on alerts ${team.data.auto ? "on" : "off"}`, `<button class="linkish" data-pf="auto">Turn ${team.data.auto ? "off" : "on"}</button>`) : "",
  ];
  return `<b>Before you present</b>${items.join("")}`;
}

/* The caption bar: follows the presenter across views */
function renderDock() {
  const dock = $("#dock"), show = demo.on && view !== "demo";
  dock.hidden = !show;
  document.body.classList.toggle("presenting", show);
  if (!show) return;
  const s = DEMO[demo.step], key = `${demo.step}|${demo.min}`;
  if (dock.dataset.key !== key) {
    dock.dataset.key = key;
    dock.classList.toggle("min", demo.min);
    $("#dk-count").textContent = `${demo.step + 1} of ${DEMO.length}`;
    $("#dk-act").textContent = demo.min ? s.title : `Act ${s.act + 1} · ${ACTS[s.act]}`;
    $("#dk-title").textContent = s.title;
    $("#dk-actions").innerHTML = stepButtons(s);
    const next = DEMO[demo.step + 1];
    $("#dk-prev").disabled = demo.step === 0;
    $("#dk-prev").innerHTML = `${icon("prev")}<span>Back</span>`;
    $("#dk-next").innerHTML = next ? `<span>Next<span class="dk-next-t">: ${esc(next.title)}</span></span>${icon("next")}` : "<span>End the demo</span>";
    $("#dk-next").className = `btn btn-sm ${s.actions ? "btn-outline" : "btn-primary"}`;
    $("#dk-min").innerHTML = icon(demo.min ? "up" : "chev");
    $("#dk-min").setAttribute("aria-expanded", String(!demo.min));
    $("#dk-min").setAttribute("aria-label", demo.min ? "Expand the demo guide" : "Collapse the demo guide");
    $("#dk-end").innerHTML = icon("x");
    $("#dk-dots").innerHTML = DEMO.map((d, i) => `<li><button data-step="${i}" aria-label="Step ${i + 1}: ${esc(d.title)}"
      ${i === demo.step ? 'aria-current="step"' : ""} title="${i + 1}. ${esc(d.title)}"></button></li>`).join("");
  }
  const say = sayOf(s), [state, text] = s.status();
  if ($("#dk-say").textContent !== say) $("#dk-say").textContent = say;
  setHTML($("#dk-status"), `${dotOf(state)}<span>${esc(text)}</span>`);
  $$("#dk-actions [data-run]").forEach((b) => {
    const a = s.actions[+b.dataset.run], working = !!a.busy?.();
    b.disabled = !a.enabled();
    if (b.dataset.busy !== String(working)) { b.dataset.busy = working; b.querySelector(".ic").outerHTML = working ? icon("loader", "spin") : icon(a.icon); }
  });
  syncPickers($("#dock"));
  $$("#dk-dots button").forEach((b) => (b.dataset.state = DEMO[+b.dataset.step].status()[0]));
}

function focusTarget(sel) {
  const el = $(sel);
  if (!el) return;
  el.scrollIntoView({behavior: reduceMotion.matches ? "auto" : "smooth", block: "start"});
  el.classList.remove("demo-focus");
  void el.offsetWidth;  // restart the highlight when the same element is shown twice
  el.classList.add("demo-focus");
  clearTimeout(focusTarget.timer);
  focusTarget.timer = setTimeout(() => el.classList.remove("demo-focus"), 2600);
}

function goStep(i) {
  demo.on = true;
  demo.step = Math.max(0, Math.min(DEMO.length - 1, i));
  saveDemo();
  const s = DEMO[demo.step];
  if (view !== s.view) { showView(s.view); history.replaceState(null, "", "#" + s.view); }
  startTeam();
  renderDock();
  setTimeout(() => focusTarget(s.target), 80);  // after the view has laid out
}

function endDemo() {
  demo.on = false;
  saveDemo();
  showView("demo");
  history.replaceState(null, "", "#demo");
  $("#demo-acts").scrollIntoView({behavior: reduceMotion.matches ? "auto" : "smooth", block: "start"});
}

function initDemo() {
  buildDemo();
  $("#demo-start").onclick = () => goStep(demo.on ? demo.step : 0);
  $("#demo-acts").onclick = (ev) => { const b = ev.target.closest("[data-go]"); if (b) goStep(+b.dataset.go); };
  $("#demo-preflight").onclick = (ev) => {
    const b = ev.target.closest("[data-pf]");
    if (!b) return;
    if (b.dataset.pf === "reset") $("#btn-reset").click();
    else if (b.dataset.pf === "stop") DEMO.at(-1).actions[0].run();
    else teamDo("Saving", async () => {
      const res = await api("/api/agents/auto", {on: !team.data?.auto});
      return `Auto-run on alerts ${res.auto ? "on" : "off"}.`;
    });
  };
  $("#dk-prev").onclick = () => goStep(demo.step - 1);
  $("#dk-next").onclick = () => (demo.step < DEMO.length - 1 ? goStep(demo.step + 1) : endDemo());
  $("#dk-end").onclick = endDemo;
  $("#dk-min").onclick = () => { demo.min = !demo.min; saveDemo(); renderDock(); };
  $("#dk-dots").onclick = (ev) => { const b = ev.target.closest("[data-step]"); if (b) goStep(+b.dataset.step); };
  $("#dk-actions").onclick = (ev) => {
    const b = ev.target.closest("[data-run]");
    if (b && !b.disabled) DEMO[demo.step].actions[+b.dataset.run].run();
  };
  $("#dk-actions").onchange = (ev) => {
    if (!ev.target.matches("[data-tc]")) return;
    $("#tc-select").value = ev.target.value;
    $("#tc-select").dispatchEvent(new Event("change"));
  };
  setInterval(() => { renderDemo(); renderDock(); }, 1000);
}
