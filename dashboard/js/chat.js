"use strict";
// Agent chat, the sidebar on the left: a team run told as the messages its agents hand each other. Each sub-agent
// reports to the Thinker, the guardrails say what they refused, the Thinker hands the Executor its plan and the
// Executor hands a person its commit. Built from the run itself (/api/agents), so earlier runs read the same. An
// agent's reasoning comes from its lines in the RAM ring, while the ring still holds them.

const chat = {open: false, w: 340, pick: "", run: null, html: "", runId: null, why: {}, asked: new Set()};
const CHAT_MIN = 280, CHAT_MAX = 560;
const KS = "kubesage", YOU = "reviewer";
const DOING = {subagent: "collecting evidence", thinker: "deciding what to do", executor: "writing the change"};

const at = (iso, ms = 0) => (Date.parse(iso) || 0) + (ms || 0);
const clock = (t) => (t ? new Date(t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "");
const sevDot = (s) => `<span class="sev"><span class="dot" style="--c:var(--${s === "critical" ? "crit" : s === "warning" ? "warn" : "muted"})"></span>${esc(s)}</span>`;
const said = (err, fallback) => `${String(err || fallback).trim().replace(/\.$/, "")}.`;  // an error as the end of a sentence
const spent = (a) => [a.label, a.ms != null ? `${(a.ms / 1000).toFixed(1)} s` : "", a.cost_usd ? usd(a.cost_usd) : ""].filter(Boolean);

function chatWho(run) {
  const who = {[KS]: {title: "KubeSage", role: "system"}, [YOU]: {title: "Reviewer", role: "person"}};
  for (const a of team.data?.team?.agents || []) who[a.name] = {title: a.title, role: a.role};
  for (const [n, a] of Object.entries(run.agents || {})) who[n] = {...who[n], ...a};
  return who;
}

// One message per hand-off, in the order they happened. `to` is an agent's name or a plain label.
function chatMessages(run, who) {
  const msgs = [], names = Object.keys(run.agents || {}), add = (m) => msgs.push(m);
  const role = (r) => names.filter((n) => who[n].role === r);
  const subs = role("subagent"), th = role("thinker")[0], ex = role("executor")[0];
  const title = (n) => who[n]?.title || n;
  const alert = run.trigger?.startsWith("alert:") ? run.trigger.slice(6) : null;
  add({key: "kick", t: at(run.started_at), from: KS, to: "the sub-agents",
    text: `${alert ? `Alert ${alert} fired.` : "Run started by hand."} ${subs.map(title).join(", ")}: collect the evidence and report to the ${title(th)}.`});

  const refused = {};  // what the guardrails dropped, per agent; a Thinker retry can list the same refusal twice
  for (const r of run.rejected || []) (refused[r.agent] ||= new Map()).set(`${r.what}|${r.reason}`, r);
  for (const n of subs) {
    const a = run.agents[n], rep = run.reports?.[n], t = at(a.started, a.ms);
    if (a.state === "failed") add({key: `sub:${n}`, t, from: n, to: th, kind: "err", agent: n, text: `I couldn't report: ${said(a.error, "the model call failed")}`});
    else if (a.state === "done") add({key: `sub:${n}`, t, from: n, to: th, agent: n, text: rep?.summary || a.summary || "No report.",
      groups: [{h: plural(rep?.findings?.length ?? a.findings ?? 0, "finding"),
        items: (rep?.findings || []).map((f) => `${sevDot(f.severity)} <b>${esc(f.title)}</b> <code>${esc(f.target)}</code>`)}],
      meta: [plural(a.signals ?? 0, "signal") + " collected", ...spent(a)]});
    if (refused[n]) add({key: `guard:${n}`, t: t + 1, from: KS, to: n, kind: "warn",
      text: `Dropped before the ${title(th)} saw it:`, groups: [{items: [...refused[n].values()].map((r) => `${esc(r.what)}: ${esc(r.reason)}`)}]});
  }

  const dg = run.diagnosis, ta = run.agents[th] || {}, tt = at(ta.started, ta.ms);
  if (refused[th]) {
    const actions = [...refused[th].values()].some((r) => r.what.startsWith("action"));
    add({key: "guard:think", t: tt - 1, from: KS, to: th, kind: "warn", text: `Refused by the guardrails${actions && run.mode === "live"
      ? ", and sent back to you once with the reasons" : ""}:`, groups: [{items: [...refused[th].values()].map((r) => `${esc(r.what)}: ${esc(r.reason)}`)}]});
  }
  if (ta.state === "failed") add({key: "think", t: tt, from: th, to: ex, kind: "err", agent: th, text: `I couldn't decide: ${said(ta.error, "the model call failed")}`});
  else if (dg && ta.state === "done") add({key: "think", t: tt, from: th, to: ex, agent: th, text: dg.summary, groups: [
    {h: plural(dg.issues.length, "issue"), items: dg.issues.map((i) => `${sevDot(i.severity)} <b>${esc(i.id)}</b> ${esc(i.title)}
      <span class="muted">${Math.round(i.confidence * 100)}% sure</span>`)},
    {h: `plan · ${plural(dg.actions.length, "action")}`, items: dg.actions.map((a) => `<b>${esc(a.id)}</b> ${esc(a.type.replace(/_/g, " "))}
      <code>${esc(a.target)}</code>${a.needs_human ? ` <span class="badge">needs a human</span>` : ""}<span class="msg-sub">${esc(a.summary)}</span>`)}],
    meta: spent(ta)});

  const x = run.executor, xa = run.agents[ex] || {}, xt = at(xa.started, xa.ms);
  if (x?.rejected?.length) add({key: "guard:exec", t: xt - 1, from: KS, to: ex, kind: "warn", text: "Refused these edits, so they aren't in the commit:",
    groups: [{items: x.rejected.map((e) => `<code>${esc(e.path)}</code> ${esc(e.action)}: ${esc(e.reason)}`)}]});
  if (xa.state === "failed") add({key: "exec", t: xt, from: ex, to: YOU, kind: "err", agent: ex, text: `I couldn't write the change: ${said(xa.error || x?.error, "it failed")}`});
  else if (x && xa.state === "done") {
    const left = (x.skipped || []).map((s) => `<b>${esc(s.action)}</b> ${esc(s.reason)}`);
    const files = {};  // one line per file, with every action that edited it
    for (const e of x.edits || []) (files[e.path] ||= new Set()).add(`${e.action} · ${e.by}`);
    add({key: "exec", t: xt, from: ex, to: YOU, agent: ex, goto: !!x.commit, branch: x.commit && x.branch,
      text: x.commit ? `Committed “${x.title}” on its own branch. Review it, then dry-run or apply it.` : "Nothing valid to change, so I committed nothing.",
      groups: [x.commit && {h: `${plural(Object.keys(files).length, "file")} changed`, items: Object.entries(files).map(([p, how]) =>
        `<code>${esc(p.replace(/^gitops\//, ""))}</code> <span class="muted">${esc([...how].join(", "))}</span>`)},
        left.length && {h: "left for a person", items: left}].filter(Boolean),
      meta: [x.commit ? `commit ${x.commit.slice(0, 8)}` : "", ...spent(xa)].filter(Boolean)});
    if (x.pr_url) add({key: "pr", t: xt + 2, from: ex, to: "GitHub", agent: ex, link: x.pr_url, text: "Opened the pull request."});
  }
  if (run.state !== "running") {
    const secs = at(run.finished_at) && at(run.started_at) ? `${Math.round((at(run.finished_at) - at(run.started_at)) / 1000)} s` : "";
    add({key: "end", sys: true, t: Math.max(at(run.finished_at), ...msgs.map((m) => m.t)) + 1, kind: run.error ? "err" : run.warning ? "warn" : "",
      text: run.error ? `Run failed: ${run.error}` : `Run ${run.state}${secs ? ` in ${secs}` : ""} · ${usd(run.cost_usd || 0)} in model calls${run.warning ? ` · ${run.warning}` : ""}`});
  }
  if (x?.applied) add({key: "apply", t: x.applied.at * 1000, from: YOU, to: "the cluster", kind: x.applied.ok ? "" : "warn",
    text: `${x.applied.ok ? "Applied" : "Applied, with errors,"} commit ${x.commit.slice(0, 8)} to the lab namespace.`});
  return msgs.sort((a, b) => a.t - b.t);
}

function chatAvatar(w) {
  const mono = w.role === "system" ? icon("hex") : w.role === "person" ? icon("person")
    : esc((w.title || "?").split(/\s+/).length > 1 ? w.title.split(/\s+/).map((s) => s[0]).join("").slice(0, 2) : w.title.slice(0, 2));
  return `<span class="av" data-role="${esc(w.role)}" aria-hidden="true">${mono}</span>`;
}

function chatMsg(m, who, run) {
  if (m.sys) return `<li class="msg sys" data-msg="${esc(m.key)}" data-kind="${m.kind || ""}"><span>${esc(m.text)}</span></li>`;
  const w = who[m.from] || {title: m.from, role: "system"}, to = who[m.to]?.title || m.to;
  const name = m.agent ? `<button class="msg-who" data-from="${esc(m.agent)}" title="Open ${esc(w.title)} in the Agent team view">${esc(w.title)}</button>`
    : `<b class="msg-who">${esc(w.title)}</b>`;
  const groups = (m.groups || []).filter((g) => g.items.length || g.h).map((g) => `<div class="msg-g">${g.h ? `<span>${esc(g.h)}</span>` : ""}
    ${g.items.length ? `<ul>${g.items.map((i) => `<li>${i}</li>`).join("")}</ul>` : ""}</div>`).join("");
  const why = m.agent && chat.why[`${run.id}/${m.agent}`];
  return `<li class="msg" data-msg="${esc(m.key)}" data-kind="${m.kind || ""}">${chatAvatar(w)}<div class="msg-b">
    <div class="msg-h">${name}${to ? `<span class="msg-to">to ${esc(to)}</span>` : ""}<time>${clock(m.t)}</time></div>
    <div class="bubble"><p>${esc(m.text)}</p>${m.branch ? `<p class="msg-sub">branch <code>${esc(m.branch)}</code></p>` : ""}${groups}
      ${m.link ? `<p><a href="${esc(m.link)}" target="_blank" rel="noopener">${esc(m.link)}</a></p>` : ""}
      ${m.goto ? `<button class="btn btn-outline btn-sm" data-pick-agent="${esc(m.agent)}">Review the change</button>` : ""}</div>
    ${m.meta?.length ? `<div class="msg-meta">${m.meta.map((x) => `<span>${esc(x)}</span>`).join(" · ")}</div>` : ""}
    ${why ? `<details class="msg-why"><summary>How it reasoned · ${why.length >= 40 ? "the last 40 lines" : plural(why.length, "line")}</summary>
      <pre>${esc(why.join("\n"))}</pre></details>` : ""}
  </div></li>`;
}

function renderChat() {
  if ($("#chat").hidden) return;
  const d = team.data, runs = d?.runs || [], pick = $("#chat-pick");
  const label = (r) => `${new Date(at(r.started_at)).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"})}
    · ${r.trigger === "manual" ? "by hand" : r.trigger.replace(/^alert:/, "")}`;
  const opts = `<option value="">Latest${d?.run ? ` · ${esc(label(d.run))}` : " run"}</option>`
    + runs.filter((r) => r.id !== d?.run?.id).map((r) => `<option value="${esc(r.id)}">${esc(label(r))}</option>`).join("");
  if (pick.dataset.html !== opts) { pick.innerHTML = opts; pick.dataset.html = opts; }
  pick.value = runs.some((r) => r.id === chat.pick) ? chat.pick : "";
  const run = chat.pick ? chat.run : d?.run, log = $("#chat-log");
  let html;
  if (!d) html = `<li class="msg sys"><span>Loading the agent team…</span></li>`;
  else if (!run) html = `<li class="chat-empty"><b>No team run yet.</b><span>When a lab alert fires, or someone presses Run agent team, the
    agents' messages to each other appear here as they happen.</span><button class="btn btn-outline btn-sm" id="chat-run">Run agent team</button></li>`;
  else {
    const who = chatWho(run), live = !chat.pick && run.state === "running";
    html = chatMessages(run, who).map((m) => chatMsg(m, who, run)).join("")
      + (live ? Object.entries(run.agents).filter(([, a]) => a.state === "running").map(([n, a]) => `<li class="msg typing" data-msg="typing:${esc(n)}">
          ${chatAvatar(who[n])}<span><b>${esc(who[n].title)}</b> is ${DOING[a.role] || "working"}<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span></span></li>`).join("") : "")
      + (!chat.pick && d.pending ? `<li class="msg sys" data-kind="warn"><span>Alert ${esc(d.pending.alert)} fired: the team starts in ${d.pending.in_s} s.</span></li>` : "");
  }
  $("#chat-stat").textContent = run?.mode || "";
  if (run) chatReasoning(run);
  if (html === chat.html) return;
  const fresh = run?.id !== chat.runId, end = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  // Keep what the reader had open and focused; follow new messages unless they've scrolled up to read.
  const active = document.activeElement, inLog = log.contains(active) && active !== log;
  const key = inLog && active.closest("[data-msg]")?.dataset.msg, part = inLog && (active.matches("summary") ? "summary" : active.matches("[data-from]") ? "[data-from]" : "[data-pick-agent]");
  const open = [...log.querySelectorAll("details[open]")].map((el) => el.closest("[data-msg]").dataset.msg);
  log.innerHTML = html;
  open.forEach((k) => { const el = log.querySelector(`[data-msg="${CSS.escape(k)}"] details`); if (el) el.open = true; });
  if (inLog) (key && log.querySelector(`[data-msg="${CSS.escape(key)}"] ${part}`) || log).focus({preventScroll: true});
  if (fresh || end) log.scrollTop = log.scrollHeight;
  chat.html = html; chat.runId = run?.id ?? null;
}

// The model's reasoning for each finished agent: the "  ⋯" lines its stream got during the agent's own time window.
// The team writes the last 40 lines of it, each cut at 220 characters.
async function chatReasoning(run) {
  for (const [n, a] of Object.entries(run.agents || {})) {
    const key = `${run.id}/${n}`;
    if (!["done", "failed"].includes(a.state) || !a.started || chat.asked.has(key)) continue;
    chat.asked.add(key);
    try {
      const res = await api(`/api/ring?streams=${encodeURIComponent(`agent/${n}`)}&tail=2000`);
      const t0 = at(a.started) / 1000 - 1, t1 = t0 + 1 + (a.ms || 0) / 1000 + 5;
      const lines = res.records.filter((r) => r.stream === `agent/${n}` && r.ts >= t0 && r.ts <= t1 && r.text.startsWith("  ⋯")).map((r) => r.text.slice(4) + (r.text.length >= 224 ? "…" : ""));
      if (lines.length) { chat.why[key] = lines; renderChat(); }
    } catch (e) { chat.asked.delete(key); }  // asked again on the next poll
  }
}

async function pickChatRun(id) {
  chat.pick = id; chat.run = null;
  if (id) try { chat.run = await api(`/api/agents/run?id=${encodeURIComponent(id)}`); } catch (e) { toast(`agent chat: ${e.message}`, "err"); chat.pick = ""; }
  renderChat();
}

function setChat(on) {
  chat.open = on; saveChat();
  $("#chat").hidden = !on;
  document.body.classList.toggle("chat-open", on);
  $("#btn-chat").setAttribute("aria-pressed", String(on));
  syncTeam();
  fitPanels();
  chat.html = "";
  renderChat();
}
function saveChat() {
  try { localStorage.setItem("kubesage-chat", JSON.stringify({open: chat.open, w: chat.w})); } catch (e) { /* storage blocked */ }
}
function sizeChat(w) {
  chat.w = Math.round(Math.min(CHAT_MAX, Math.max(CHAT_MIN, w)));
  document.documentElement.style.setProperty("--chat-w", `${chat.w}px`);
  $("#chat-grip").setAttribute("aria-valuemax", Math.max(CHAT_MIN, Math.floor(innerWidth * 0.34)));
  fitPanels();  // the width it gets, within the room the view needs
}

function initChat() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("kubesage-chat") || "{}"); } catch (e) { /* storage blocked */ }
  if (Number.isFinite(saved.w)) chat.w = saved.w;
  sizeChat(chat.w);
  $("#btn-chat").innerHTML = `${icon("chat")}<span>Agent chat</span>`;
  $("#chat-x").innerHTML = icon("x");
  $("#btn-chat").onclick = () => setChat(!chat.open);
  // Closing returns focus to a button that opens it again: the header's, or on a phone the Agent team view's.
  const close = () => { setChat(false); [$("#btn-chat"), $("#team-runbar [data-chat]")].find((b) => b?.offsetParent)?.focus(); };
  $("#chat-x").onclick = close;
  $("#chat").onkeydown = (ev) => { if (ev.key === "Escape" && innerWidth <= 900) close(); };
  $("#chat-pick").onchange = (ev) => pickChatRun(ev.currentTarget.value);
  $("#chat-log").onclick = (ev) => {
    if (ev.target.closest("#chat-run")) { teamRun(); return; }
    const b = ev.target.closest("[data-from], [data-pick-agent]");
    if (!b) return;
    team.sel = b.dataset.from || b.dataset.pickAgent;
    if (chat.pick) toast("The Agent team view shows the latest run.", "warn");
    if (view === "agents") renderTeam(); else location.hash = "agents";
    if (innerWidth <= 900) setChat(false);
    $("#team-detail").scrollIntoView({block: "start", behavior: reduceMotion.matches ? "auto" : "smooth"});
  };
  const grip = $("#chat-grip");
  grip.onpointerdown = (ev) => { grip.setPointerCapture(ev.pointerId); document.body.classList.add("resizing"); };
  grip.onpointermove = (ev) => { if (grip.hasPointerCapture(ev.pointerId)) sizeChat(ev.clientX); };
  grip.onpointerup = grip.onpointercancel = () => { document.body.classList.remove("resizing"); saveChat(); };
  grip.onkeydown = (ev) => {
    const step = {ArrowRight: 40, ArrowLeft: -40}[ev.key];
    if (!step) return;
    ev.preventDefault();
    sizeChat(+grip.getAttribute("aria-valuenow") + step);
    saveChat();
  };
  // Open by default where there's room for it beside a view; a reader's own choice wins after that.
  setChat(typeof saved.open === "boolean" ? saved.open : innerWidth >= 1280);
}
