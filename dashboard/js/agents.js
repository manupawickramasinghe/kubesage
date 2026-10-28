"use strict";
// Agent team: test cases, the team run, its diagnosis and change.

/* ---------- Agent team and live test cases (Lab), and the tmux view: a read-only mirror of the RAM ring ---------- */

const team = {data: null, tcs: null, sel: null, busy: false, timers: [], fast: 0};

function teamMsg(text, kind = "") {
  const el = $("#team-msg");
  el.textContent = text; el.dataset.kind = kind;
  if (text && kind !== "busy" && !inOpenView(el)) toast(text, kind || "ok");  // e.g. a case started from the tmux view
}

function tcOptions() {
  const cat = team.tcs?.catalog || [], active = team.tcs?.active || {};
  const html = cat.map((t) => `<option value="${esc(t.name)}">${esc(t.title)}${active[t.name] ? " (running)" : ""}</option>`).join("");
  const running = Object.keys(active)[0];
  for (const sel of ["#tc-select", "#tx-tc"]) {
    // The demo's default incident is cpu-code: its fix is a code change, which more CPU alone can't solve.
    const el = $(sel), v = team.pick || running || (el.dataset.html ? el.value : "cpu-code");
    if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
    if (v && cat.some((t) => t.name === v)) el.value = v;
  }
}

function renderTestcases() {
  tcOptions();
  const name = $("#tc-select").value, tc = (team.tcs?.catalog || []).find((t) => t.name === name);
  $("#tc-desc").innerHTML = tc ? `<b>${esc(tc.title)}.</b> ${esc(tc.summary)}<span>Expected fix: ${esc(tc.expect)}</span>` : "";
  const active = team.tcs?.active || {}, alerts = team.tcs?.alerts || [];
  const chips = Object.entries(active).map(([n, a]) => `<span class="badge"><span class="pulse-dot" style="--c:var(--rec)"></span>${esc(n)}:
    ${esc(Object.entries(a.phases).map(([k, v]) => `${v} ${k}`).join(", "))}${a.restarts ? ` · ${a.restarts} restarts` : ""}${a.stopping ? " · stopping" : ""}</span>`);
  chips.push(...alerts.map((a) => `<span class="badge firing ${a.severity === "critical" ? "crit" : ""}" title="${esc(a.summary || "")}">
    ${icon("zap")}${esc(a.name)}${a.pod ? ` · ${esc(a.pod)}` : a.node ? ` · ${esc(a.node)}` : ""}</span>`));
  if (team.data?.pending) chips.push(`<span class="badge">team runs in ${team.data.pending.in_s} s (alert ${esc(team.data.pending.alert)})</span>`);
  replaceHTML($("#tc-live"), chips.join("") || `<span>${team.tcs?.cluster === false ? "Cluster unreachable." : "Nothing running · no lab alerts firing"}</span>`);
  const running = !!active[name];
  for (const [start, stop] of [["#tc-start", "#tc-stop"], ["#tx-start", "#tx-stop"]]) {
    $(start).disabled = team.busy || running || !tc;
    $(stop).disabled = team.busy || !running;
  }
  const teamRunning = team.data?.run?.state === "running";
  $("#team-run").disabled = $("#tx-run").disabled = team.busy || teamRunning;
  $("#team-auto").checked = !!team.data?.auto;
}

function agentStats(a) {
  const n = (v, one, many) => `${v ?? "–"} ${v === 1 ? one : many}`;
  const counts = a.role === "thinker" ? [n(a.findings, "issue", "issues")] : a.role === "executor" ? [n(a.findings, "edit", "edits")]
    : [n(a.signals, "signal", "signals"), n(a.findings, "finding", "findings")];
  const hot = (c, i) => i === counts.length - 1 && a.findings ? `<b>${esc(c)}</b>` : esc(c);
  return `<span>${counts.map(hot).join(" · ")}</span><span>${a.ms != null ? `${(a.ms / 1000).toFixed(1)} s · ` : ""}${usd(a.cost_usd || 0)}</span>`;
}

function agentCard(name, a, big) {
  const state = a.state || "idle", idle = ["queued", "idle", "skipped"].includes(state);
  const sum = a.error ? `<span class="agent-sum err">${esc(a.error)}</span>`
    : idle || !a.summary ? `<span class="agent-sum idle">${esc({queued: "Queued…", skipped: "Not run: an earlier step failed."}[state] || a.description || "")}</span>`
    : `<span class="agent-sum">${esc(a.summary)}</span>`;
  return `<button class="agent ${big ? "big" : ""}" data-agent="${esc(name)}" data-state="${esc(state)}" aria-pressed="${team.sel === name}"
    title="${esc(a.description || "")}">
    <span class="agent-top"><span class="agent-led" aria-hidden="true"></span><b>${esc(a.title || name)}</b>
      <span class="sr-only">state ${esc(state)}</span><span class="agent-size">${esc(a.size || "")}</span></span>
    <span class="agent-model" title="${esc(a.model || "")}">${esc(a.label || a.model || "")}</span>
    ${idle ? "" : `<span class="agent-stats">${state === "running" ? "<span>running…</span>" : agentStats(a)}</span>`}
    ${sum}
  </button>`;
}

function runBar(run) {
  if (!run) return `<span>No team run yet. Start a test case and wait for its alert, or press <b>Run agent team</b>.</span>`;
  const st = run.state === "done" ? (run.error ? "crit" : run.warning ? "warn" : "good") : run.state === "running" ? "rec" : "crit";
  const t0 = Date.parse(run.started_at), t1 = Date.parse(run.finished_at);  // ISO 8601 from run.json
  const secs = t0 && t1 ? `${((t1 - t0) / 1000).toFixed(0)} s` : "";
  const when = t0 ? new Date(t0).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "";
  return `<span class="badge"><span class="${run.state === "running" ? "pulse-dot" : "dot"}" style="--c:var(--${st})"></span>${esc(run.state)}</span>
    <span>Run <code>${esc(run.id)}</code></span><span>${esc(run.trigger === "manual" ? "started by hand" : run.trigger)}${when ? ` at ${when}` : ""}</span>
    <span>${esc(run.mode)} · ${esc(run.profile)}</span>${secs ? `<span class="num-v">${secs}</span>` : ""}
    <span class="num-v">${usd(run.cost_usd || 0)}</span>${run.error ? `<span style="color:var(--crit)">${esc(run.error)}</span>`
      : run.warning ? `<span>${esc(run.warning)}</span>` : ""}
    <span class="sp"></span><button class="btn btn-ghost btn-sm" data-goto="tmux">Follow in tmux</button>`;
}

function renderTeam() {
  const d = team.data;
  if (!d) return;
  const run = d.run, agents = {};
  (d.team.agents || []).forEach((a) => (agents[a.name] = {...a, state: "idle"}));
  if (run) Object.entries(run.agents || {}).forEach(([n, a]) => (agents[n] = {...agents[n], ...a,
    state: a.state === "queued" && run.state !== "running" ? "skipped" : a.state}));
  const by = (role) => Object.entries(agents).filter(([, a]) => a.role === role);
  const arrow = (role) => by(role).some(([, a]) => a.state === "running") ? "flowing" : by(role).some(([, a]) => a.state === "done") ? "passed" : "";
  const col = (cls, head, cards) => `<div class="team-col ${cls}"><span class="team-col-h">${head}</span>${cards}</div>`;
  replaceHTML($("#team-flow"), d.team.error ? `<p class="team-msg" data-kind="err">agents/: ${esc(d.team.error)}</p>` :
    col(`subs ${arrow("thinker")}`, "1 · Sub-agents collect", `<div class="agents">${by("subagent").map(([n, a]) => agentCard(n, a)).join("")}</div>`)
    + col(`mid ${arrow("executor")}`, "2 · Thinker decides", by("thinker").map(([n, a]) => agentCard(n, a, true)).join(""))
    + col("last", "3 · Executor changes", by("executor").map(([n, a]) => agentCard(n, a, true)).join("")));
  replaceHTML($("#team-runbar"), runBar(run));
  renderTeamDetail(run, agents);
  const rules = d.team.rules || [];
  const html = rules.length ? rules.map((r) => `<pre><b>${esc(r.file)}</b>\n${esc(r.text)}</pre>`).join("") : `<p class="sub">No rule files.</p>`;
  if ($("#team-rules").dataset.html !== html) { $("#team-rules").innerHTML = html; $("#team-rules").dataset.html = html; }
}

function sevBadge(sev) {
  const st = sev === "critical" ? "crit" : sev === "warning" ? "warn" : "good";
  return `<span class="badge"><span class="dot" style="--c:var(--${st})"></span>${esc(sev)}</span>`;
}

function diffHTML(text) {
  const out = [];
  for (const l of text.replace(/\n+$/, "").split("\n")) {
    const file = l.match(/^diff --git a\/\S+ b\/(\S+)/);
    if (file) { out.push(`<span class="ln file">${esc(file[1])}</span>`); continue; }
    if (/^(index [0-9a-f]|--- (a\/|\/dev\/null)|\+\+\+ (b\/|\/dev\/null))/.test(l)) continue;
    if (/^(new|deleted) file mode/.test(l) && out.length) { out[out.length - 1] = out.at(-1).replace("</span>", `<em>${l.split(" ")[0]} file</em></span>`); continue; }
    const cls = l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : l.startsWith("\\") ? "meta" : "";
    out.push(`<span class="ln ${cls}">${esc(l) || " "}</span>`);
  }
  return out.join("");
}

const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const actionRow = (a) => `<li class="act"><span class="act-id">${esc(a.id)}</span><span class="act-main">
  <span class="tag">${esc(a.type.replace(/_/g, " "))}</span><code>${esc(a.target)}</code><span>${esc(a.summary)}</span>
  ${a.params?.monthly_delta_usd != null ? `<b>${a.params.monthly_delta_usd >= 0 ? "+" : ""}${usd(a.params.monthly_delta_usd)}/mo</b>` : ""}
  ${a.needs_human ? `<span class="badge">needs a human</span>` : ""}</span>${a.rationale ? `<span class="act-why">${esc(a.rationale)}</span>` : ""}</li>`;

function renderTeamDetail(run, agents) {
  const box = $("#team-detail");
  if (!run) { replaceHTML(box, `<p class="sub">The diagnosis and the change appear here after a run.</p>`); return; }
  const sel = team.sel && agents[team.sel];
  const head = (title, meta) => `<div class="det-h"><h3>${esc(title)}</h3>${meta ? `<span>${esc(meta)}</span>` : ""}</div>`;
  let html = "";
  if (sel && sel.role === "subagent") {
    const rep = run.reports?.[team.sel], sigs = (run.signals?.[team.sel] || []).filter((x) => x.severity);
    html = head(sel.title, `${sel.label} · ${plural(sel.signals ?? 0, "signal")} · ${plural(rep?.findings?.length ?? 0, "finding")}`)
      + `<p class="det-sum">${esc(rep?.summary || sel.error || "No report.")}</p>
      ${(rep?.findings || []).map((f) => `<div class="issue"><div class="issue-top">${sevBadge(f.severity)}<span>${esc(f.title)}</span><code>${esc(f.target)}</code></div>
        <p>${esc(f.summary)}</p><p class="sub">cites ${f.signal_ids.map((i) => `<code>${esc(i)}</code>`).join(" ")}</p></div>`).join("") || `<p class="sub">No findings.</p>`}
      ${sigs.length ? head("Signals over threshold", plural(sigs.length, "signal"))
        + `<ul class="sub">${sigs.map((x) => `<li><code>${esc(x.id)}</code> ${esc(x.kind)} = ${esc(x.value)} ${esc(x.unit)} · ${esc(x.detail)}</li>`).join("")}</ul>` : ""}`;
  } else {
    const dg = run.diagnosis, th = agents.thinker || {};
    if (!sel || sel.role === "thinker") {
      html += dg ? head("Diagnosis", `Thinker · ${th.label || ""} · ${plural(dg.issues.length, "issue")}, ${plural(dg.actions.length, "action")}`)
        + `<p class="det-sum">${esc(dg.summary)}</p>`
        + dg.issues.map((i) => `<div class="issue"><div class="issue-top">${sevBadge(i.severity)}<span>${esc(i.id)} · ${esc(i.title)}</span>
          <span class="conf">${Math.round(i.confidence * 100)}% sure</span></div><p>${esc(i.root_cause)}</p>
          <ul class="acts">${dg.actions.filter((a) => a.issue === i.id).map(actionRow).join("")}</ul></div>`).join("")
        : head("Diagnosis", `Thinker · ${th.label || ""}`)
          + `<p class="sub">${run.state === "running" ? "The Thinker is waiting for the sub-agents…" : esc(run.error || "No diagnosis.")}</p>`;
      if ((run.rejected || []).length) html += `<details><summary class="sub">${plural(run.rejected.length, "item")} rejected by validation</summary>
        <ul class="sub">${run.rejected.map((r) => `<li>${esc(r.agent)}: ${esc(r.what)}: ${esc(r.reason)}</li>`).join("")}</ul></details>`;
    }
    if (!sel || sel.role === "executor") {
      const ex = run.executor, xa = agents.executor || {};
      if (ex) {
        const prBtn = ex.pr_url ? `<a class="btn btn-outline btn-sm" href="${esc(ex.pr_url)}" target="_blank" rel="noopener">${icon("pr")}View PR</a>`
          : team.data.prs ? `<button class="btn btn-primary btn-sm" data-exec="pr" ${ex.commit ? "" : "disabled"}>${icon("pr")}Open pull request</button>`
          : `<span class="sub">PRs are off: start the server with <code>--agent-prs</code></span>`;
        const at = ex.applied ? new Date(ex.applied.at * 1000).toLocaleTimeString() : "";
        html += head("Change", `Executor · ${xa.label || ""}${ex.edits?.length ? ` · ${plural(ex.edits.length, "file")}` : ""}`)
          + (ex.error ? `<p class="team-msg" data-kind="err">${esc(ex.error)}</p>` : "")
          + (ex.commit ? `<p class="ex-title">${esc(ex.title)}</p>
            <p class="ex-meta"><span>branch <code>${esc(ex.branch)}</code></span><span>from <code>origin/${esc(ex.base)}</code></span>
              <span>commit <code>${esc(ex.commit.slice(0, 8))}</code></span>${ex.applied ? `<span>${ex.applied.ok ? "applied" : "applied with errors"} at ${at}</span>` : ""}</p>
            <div class="exec-bar"><button class="btn btn-outline btn-sm" data-exec="dry">Dry-run apply</button>
              <button class="btn btn-outline btn-sm" data-exec="apply">Apply to cluster</button>${prBtn}</div>
            <ul class="files">${ex.edits.map((e) => `<li title="${esc(e.path)}"><code>${esc(e.path.replace(/^gitops\//, ""))}</code>${esc(e.action)} · ${esc(e.by)}</li>`).join("")}</ul>
            <pre class="diff">${diffHTML(ex.diff || "")}</pre>`
            : `<p class="sub">${run.state === "running" ? "Waiting for the Thinker…" : "No commit: nothing valid to change."}</p>`)
          + (ex.skipped?.length ? `<p class="sub">Not changed: ${ex.skipped.map((x) => `${esc(x.action)}: ${esc(x.reason)}`).join("; ")}</p>` : "")
          + (ex.rejected?.length ? `<p class="sub">Rejected edits: ${ex.rejected.map((x) => `<code>${esc(x.path)}</code> ${esc(x.reason)}`).join("; ")}</p>` : "");
      } else if (run.state === "running" && !dg) {
        html += head("Change", `Executor · ${xa.label || ""}`) + `<p class="sub">Waiting for the Thinker…</p>`;
      }
    }
  }
  replaceHTML(box, html);
}

async function pollTeam() {
  try { team.data = await api("/api/agents"); } catch (e) { teamMsg(`agents: ${e.message}`, "err"); return; }
  if (!$("#team").hidden) renderTeam();
  renderTestcases();
  if (tx.loaded && JSON.stringify(txWindows().map((w) => w.panes.length)) !== tx.shape) renderTx();
  else if (tx.loaded) txStates();
}
async function pollTestcases() {
  try { team.tcs = await api("/api/testcases"); } catch (e) { team.tcs = {catalog: team.tcs?.catalog || [], active: {}, alerts: [], cluster: false}; }
  renderTestcases();
}

async function teamDo(label, fn) {
  if (team.busy) return;
  team.busy = true; renderTestcases(); $("#team-msg").dataset.kind = "busy"; $("#team-msg").textContent = label + "…";
  try {
    const msg = await fn();
    teamMsg(msg || "", "");
    setTimeout(() => { if ($("#team-msg").textContent === msg) teamMsg(""); }, 20000);
  }
  catch (e) { teamMsg(e.message, "err"); }
  finally { team.busy = false; await Promise.all([pollTestcases(), pollTeam()]); }
}

const tcAction = (action, name) => teamDo(`${action} ${name || "all"}`, async () => {
  const res = await api("/api/testcase", {action, name});
  return `${action === "start" ? "Started" : "Stopped"} ${name}: ${res.steps.map((x) => x.cmd).join(" · ")}`;
});
const teamRun = () => teamDo("Starting the agent team", async () => {
  const res = await api("/api/agents/run", {trigger: "manual"});
  team.fast = Date.now() + 120000;
  return `Team run ${res.id} started. Follow it here or in the tmux view.`;
});

async function execAction(kind) {
  const run = team.data?.run;
  if (!run) return;
  if (kind === "apply" && !confirm(`Apply the Executor's commit ${run.executor.commit.slice(0, 8)} to the live cluster? `
    + "This runs kubectl apply for the changed app directories in the lab namespace. Terraform changes are not applied.")) return;
  await teamDo(kind === "pr" ? "Opening the pull request" : kind === "dry" ? "Dry-run apply" : "Applying", async () => {
    if (kind === "pr") { const res = await api("/api/agents/pr", {id: run.id}); return `Opened ${res.pr_url}`; }
    const res = await api("/api/agents/apply", {id: run.id, dry_run: kind === "dry"});
    return `${kind === "dry" ? "Dry run" : "Applied"}: ${res.steps.map((x) => `${x.cmd} ${x.ok ? "ok" : "FAILED"}`).join(" · ")}`
      + (res.skipped.length ? ` · not applied: ${res.skipped.map((x) => x.path).join(", ")} (Terraform)` : "");
  });
}


function startTeam() {
  if (team.timers.length) return;
  pollTestcases(); pollTeam();
  team.timers = [setInterval(() => (Date.now() < team.fast || team.data?.run?.state === "running" || team.data?.pending ? pollTeam() : null), 1500),
    setInterval(pollTeam, 6000), setInterval(pollTestcases, 5000)];
}
function stopTeam() { team.timers.forEach(clearInterval); team.timers = []; }

function initTeam() {
  const sync = (from, to) => $(from).addEventListener("change", () => { $(to).value = $(from).value; team.pick = $(from).value; renderTestcases(); });
  sync("#tc-select", "#tx-tc"); sync("#tx-tc", "#tc-select");
  $("#tc-start").onclick = () => tcAction("start", $("#tc-select").value);
  $("#tc-stop").onclick = () => tcAction("stop", $("#tc-select").value);
  $("#tx-start").onclick = () => tcAction("start", $("#tx-tc").value);
  $("#tx-stop").onclick = () => tcAction("stop", $("#tx-tc").value);
  $("#team-run").onclick = $("#tx-run").onclick = teamRun;
  $("#team-auto").onchange = (ev) => teamDo("Saving", async () => {
    const res = await api("/api/agents/auto", {on: ev.currentTarget.checked});
    return `Auto-run on alerts ${res.auto ? "on" : "off"}.`;
  });
  $("#team-flow").onclick = (ev) => {
    const b = ev.target.closest("[data-agent]");
    if (!b) return;
    team.sel = team.sel === b.dataset.agent ? null : b.dataset.agent;
    renderTeam();
  };
  $("#team-detail").onclick = (ev) => { const b = ev.target.closest("[data-exec]"); if (b) execAction(b.dataset.exec); };
  $("#team-runbar").onclick = (ev) => { const b = ev.target.closest("[data-goto]"); if (b) location.hash = b.dataset.goto; };
  $("#tx-windows").onclick = (ev) => { const b = ev.target.closest("[data-win]"); if (b) { tx.win = +b.dataset.win; renderTx(); } };
  $("#tx-range").onchange = (ev) => ($("#tx-save").href = `/api/ring/dump?minutes=${ev.currentTarget.value}`);
  $("#tx-pause").onclick = (ev) => {
    tx.paused = !tx.paused;
    ev.currentTarget.setAttribute("aria-pressed", String(tx.paused));
    ev.currentTarget.textContent = tx.paused ? "resume" : "pause";
  };
}
