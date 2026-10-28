"use strict";
// tmux view: a read-only mirror of the RAM ring.

const tx = {last: 0, recs: [], win: 0, paused: false, polling: false, stat: null, timers: [], loaded: false};

/* tmux view */
function txWindows() {
  const agents = (team.data?.team?.agents || []).map((a) => a.name);
  return [
    {name: "agents", panes: (agents.length ? agents : ["team"]).map((n) => ({title: n, agent: n, streams: [`agent/${n}`]}))},
    {name: "testcases", panes: [{title: "test cases · alerts · team", streams: ["testcase/", "alerts", "agent/team"]},
      {title: "kubectl get pods,events -n lab -w", streams: ["watch/"]}]},
    {name: "journal", panes: [{title: "journal", streams: ["journal"]}, {title: "access log (4xx, refused)", streams: ["access"]}]},
    {name: "ops", panes: [{title: "ops shell (kubesage tmux, piped with pipe-pane)", streams: ["tmux/"]}]},
  ];
}
const txMatch = (stream, streams) => streams.some((s) => s.endsWith("/") ? stream.startsWith(s) : stream === s);
function txClass(t) {
  if (/^ {2}\+(?!\+\+)/.test(t)) return "tx-ok";
  if (/^ {2}-(?!--)/.test(t)) return "tx-err";
  if (/^ {2}@@/.test(t)) return "tx-cy";
  if (t.startsWith("✗") || /\bFAILED\b|\[critical\]|OOMKilled|BackOff/.test(t)) return "tx-err";
  if (t.startsWith("✓") || /-> ok$/.test(t)) return "tx-ok";
  if (t.startsWith("━━")) return "tx-head";
  if (t.includes("⋯")) return "tx-think";
  if (/^\s*[◆●]|\[warning\]|^FIRING/.test(t)) return "tx-hi";
  if (/^\s*[→✎▶↳↺]/.test(t)) return "tx-cy";
  return "";
}
function txLine(r) {
  const d = document.createElement("div"), t = document.createElement("span");
  t.className = "t";
  t.textContent = new Date(r.ts * 1000).toTimeString().slice(0, 8) + " ";
  const body = document.createElement("span");
  body.className = txClass(r.text);
  // kubectl -w pads its columns for a full terminal; two spaces keep an event on one line in a pane.
  const text = r.stream.startsWith("watch/") ? r.text.replace(/ {3,}/g, "  ") : r.text;
  body.textContent = r.stream.startsWith("agent/") || r.stream.startsWith("tmux/") ? text : `${r.stream.replace(/^(testcase|watch)\//, "")}  ${text}`;
  d.append(t, body);
  return d;
}
function renderTx() {
  const wins = txWindows();
  tx.shape = JSON.stringify(wins.map((w) => w.panes.length));
  tx.win = Math.min(tx.win, wins.length - 1);
  $("#tx-windows").innerHTML = wins.map((w, i) => `<button role="tab" data-win="${i}" aria-selected="${i === tx.win}">${i}:${esc(w.name)}${i === tx.win ? "*" : ""}</button>`).join("");
  const w = wins[tx.win], box = $("#tx-win");
  box.dataset.n = w.panes.length;
  box.replaceChildren(...w.panes.map((p, i) => {
    const pane = document.createElement("div");
    pane.className = "pane on";  // "on": the lab's single-pane phone layout hides the others
    pane.innerHTML = `<div class="pane-h"><b>${tx.win}.${i}</b><span>${esc(p.title)}</span><span class="sp"></span>${p.agent ? `<span class="st"></span>` : ""}</div>
      <div class="tx-lines" role="log" aria-label="${esc(p.title)}"></div>`;
    const lines = pane.querySelector(".tx-lines"), mine = tx.recs.filter((r) => txMatch(r.stream, p.streams)).slice(-800);
    lines.append(...mine.map(txLine));
    if (!mine.length) lines.append(Object.assign(document.createElement("div"), {className: "tx-dim", textContent: "(nothing in the ring for this pane yet)"}));
    pane._streams = p.streams;
    pane._agent = p.agent;
    return pane;
  }));
  box.querySelectorAll(".tx-lines").forEach((l) => (l.scrollTop = l.scrollHeight));
  txStates();
}
// Agent panes show "title · model" and the agent's state in the last run, like a tmux pane title.
function txStates() {
  const info = Object.fromEntries((team.data?.team?.agents || []).map((a) => [a.name, a])), run = team.data?.run?.agents || {};
  $$("#tx-win .pane").forEach((pane) => {
    const a = info[pane._agent];
    if (!a) return;
    pane.querySelector(".pane-h > span").textContent = `${a.title} · ${a.label || a.model}`;
    const st = pane.querySelector(".st"), state = run[pane._agent]?.state || "idle";
    st.className = `st ${state}`;
    st.textContent = state;
  });
}
function txAppend(recs) {
  $$("#tx-win .pane").forEach((pane) => {
    const lines = pane.querySelector(".tx-lines"), mine = recs.filter((r) => txMatch(r.stream, pane._streams));
    if (!mine.length) return;
    const stick = lines.scrollTop + lines.clientHeight >= lines.scrollHeight - 40;
    lines.querySelector(".tx-dim:only-child")?.remove();
    lines.append(...mine.map(txLine));
    while (lines.children.length > 1200) lines.firstElementChild.remove();
    if (stick && !tx.paused) lines.scrollTop = lines.scrollHeight;
  });
}
async function pollRing() {
  if (tx.polling) return;
  tx.polling = true;
  try {
    const res = await api(tx.loaded ? `/api/ring?after=${tx.last}&limit=3000` : "/api/ring?tail=5000");
    if (tx.loaded && res.last < tx.last) { tx.loaded = false; tx.recs = []; tx.last = 0; return; }  // a new ring
    tx.last = res.last;
    tx.recs.push(...res.records);
    if (tx.recs.length > 20000) tx.recs.splice(0, tx.recs.length - 20000);
    if (!tx.loaded) { tx.loaded = true; renderTx(); } else if (res.records.length) txAppend(res.records);
  } catch (e) {
    $("#tx-ring").textContent = `ring: ${e.message}`;
  } finally { tx.polling = false; }
}
async function pollRingStat() {
  try {
    const st = await api("/api/ring/stat");
    $("#tx-ring").textContent = `ring ${(st.used_bytes / 2 ** 20).toFixed(1)} / ${(st.capacity_bytes / 2 ** 20).toFixed(0)} MiB in RAM · `
      + `${st.records.toLocaleString()} lines${st.wrapped ? " · wrapped" : ""}${st.oldest_ts ? ` · since ${new Date(st.oldest_ts * 1000).toLocaleTimeString()}` : ""}`;
  } catch (e) { $("#tx-ring").textContent = `ring: ${e.message}`; }
  if (!lab.status) lab.status = await api("/api/lab").catch(() => null);  // the host name, when the lab view hasn't loaded it
  $("#tx-clock").textContent = `"${lab.status?.host || "kubesage"}" ${new Date().toTimeString().slice(0, 5)}`;
}

function startTx() {
  if (tx.timers.length) return;
  pollRing(); pollRingStat();
  tx.timers = [setInterval(pollRing, 1000), setInterval(pollRingStat, 5000)];
}
function stopTx() { tx.timers.forEach(clearInterval); tx.timers = []; }
