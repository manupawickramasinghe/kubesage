"use strict";
// tmux view: a read-only mirror of the RAM ring, full size in its own view and as a panel beside the others.

const tx = {last: 0, recs: [], paused: false, polling: false, stat: null, timers: [], loaded: false,
  side: false, sideW: 480, back: "agents", shown: new Set(), at: {main: {win: 0, zoom: null}, side: {win: 0, zoom: null}}};
// The two places the mirror draws into. Each keeps its own window and zoomed pane.
const TX_EL = {main: {panel: "#tmuxview", tabs: "#tx-windows", box: "#tx-win", clock: "#tx-clock"},
  side: {panel: "#txside", tabs: "#txs-windows", box: "#txs-win", clock: "#txs-clock"}};
const SIDE_MIN = 320, SIDE_MAX = 960;  // CSS also caps the panel at 60% of the window

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
const txShown = () => Object.keys(TX_EL).filter((k) => !$(TX_EL[k].panel).hidden);
const txPanes = () => txShown().flatMap((k) => [...$$(`${TX_EL[k].box} .pane`)]);
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
// Draws one place (or every place on screen) from the records held so far.
function renderTx(only) {
  const wins = txWindows();
  tx.shape = JSON.stringify(wins.map((w) => w.panes.length));
  for (const k of only ? [only] : txShown()) {
    const at = tx.at[k], tabs = $(TX_EL[k].tabs), box = $(TX_EL[k].box), refocus = tabs.contains(document.activeElement);
    at.win = Math.min(at.win, wins.length - 1);
    const w = wins[at.win];
    if (at.zoom != null && at.zoom >= w.panes.length) at.zoom = null;
    box.dataset.n = w.panes.length;
    box.replaceChildren(...w.panes.map((p, i) => {
      const pane = document.createElement("div");
      pane.className = "pane on";  // "on": the lab's single-pane phone layout hides the others
      pane.innerHTML = `<div class="pane-h"><b>${at.win}.${i}</b><span>${esc(p.title)}</span><span class="sp"></span>${p.agent ? `<span class="st"></span>` : ""}
        ${w.panes.length > 1 ? `<button class="tbtn" data-zoom="${i}" aria-pressed="false" title="Fill the window with this pane (tmux: prefix z)">zoom</button>` : ""}</div>
        <div class="tx-lines" role="log" aria-label="${esc(p.title)}"></div>`;
      const lines = pane.querySelector(".tx-lines"), mine = tx.recs.filter((r) => txMatch(r.stream, p.streams)).slice(-800);
      lines.append(...mine.map(txLine));
      if (!mine.length) lines.append(Object.assign(document.createElement("div"), {className: "tx-dim", textContent: "(nothing in the ring for this pane yet)"}));
      pane._streams = p.streams;
      pane._agent = p.agent;
      return pane;
    }));
    txZoom(k);
    txTabs(k, wins);
    box.querySelectorAll(".tx-lines").forEach((l) => (l.scrollTop = l.scrollHeight));
    if (refocus) tabs.querySelector('[aria-selected="true"]').focus();
  }
  txStates();
}
// The status bar's window list, with tmux's flags: * for the current window, Z when a pane is zoomed.
function txTabs(k, wins = txWindows()) {
  const at = tx.at[k];
  $(TX_EL[k].tabs).innerHTML = wins.map((w, i) => {
    const on = i === at.win;
    return `<button role="tab" data-win="${i}" aria-selected="${on}">${i}:${esc(w.name)}${on ? "*" : ""}${on && at.zoom != null ? "Z" : ""}</button>`;
  }).join("");
}
function txZoom(k) {
  const at = tx.at[k], box = $(TX_EL[k].box);
  if (at.zoom == null) delete box.dataset.zoomed; else box.dataset.zoomed = at.zoom;
  [...box.children].forEach((pane, i) => {
    pane.classList.toggle("zoomed", i === at.zoom);
    pane.querySelector("[data-zoom]")?.setAttribute("aria-pressed", String(i === at.zoom));
  });
}
// Agent panes show "title · model" and the agent's state in the last run, like a tmux pane title.
function txStates() {
  const info = Object.fromEntries((team.data?.team?.agents || []).map((a) => [a.name, a])), run = team.data?.run?.agents || {};
  txPanes().forEach((pane) => {
    const a = info[pane._agent];
    if (!a) return;
    pane.querySelector(".pane-h > span").textContent = `${a.title} · ${a.label || a.model}`;
    const st = pane.querySelector(".st"), state = run[pane._agent]?.state || "idle";
    st.className = `st ${state}`;
    st.textContent = state;
  });
}
function txAppend(recs) {
  txPanes().forEach((pane) => {
    const lines = pane.querySelector(".tx-lines"), mine = recs.filter((r) => txMatch(r.stream, pane._streams));
    if (!mine.length) return;
    const stick = lines.scrollTop + lines.clientHeight >= lines.scrollHeight - 40;
    lines.querySelector(".tx-dim:only-child")?.remove();
    lines.append(...mine.map(txLine));
    while (lines.children.length > 1200) lines.firstElementChild.remove();
    if (stick && !tx.paused) lines.scrollTop = lines.scrollHeight;
  });
}
// The first load takes each feed's own tail, so a quiet pane (an agent between runs) isn't pushed out of view by a
// chatty one (kubectl -w, a crash-looping test case). Later polls take everything after the newest record seen.
const TX_FEEDS = {"agent/": 3000, "testcase/": 800, alerts: 800, "watch/": 800, journal: 800, access: 800, "tmux/": 800};
async function pollRing() {
  if (tx.polling) return;
  tx.polling = true;
  try {
    if (!tx.loaded) {
      const tails = await Promise.all(Object.entries(TX_FEEDS).map(([s, n]) => api(`/api/ring?tail=${n}&streams=${encodeURIComponent(s)}`)));
      tx.recs = [...new Map(tails.flatMap((t) => t.records).map((r) => [r.seq, r])).values()].sort((a, b) => a.seq - b.seq);
      tx.last = Math.max(...tails.map((t) => t.last));
      tx.loaded = true;
      renderTx();
      return;
    }
    const res = await api(`/api/ring?after=${tx.last}&limit=3000`);
    if (res.last < tx.last) { tx.loaded = false; tx.recs = []; tx.last = 0; return; }  // a new ring
    tx.last = res.last;
    tx.recs.push(...res.records);
    if (tx.recs.length > 20000) tx.recs.splice(0, tx.recs.length - 20000);
    if (res.records.length) txAppend(res.records);
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
  Object.values(TX_EL).forEach((el) => ($(el.clock).textContent = `"${lab.status?.host || "kubesage"}" ${new Date().toTimeString().slice(0, 5)}`));
}

function startTx() {
  if (tx.timers.length) return;
  pollRing(); pollRingStat();
  tx.timers = [setInterval(pollRing, 1000), setInterval(pollRingStat, 5000)];
}
function stopTx() { tx.timers.forEach(clearInterval); tx.timers = []; }

/* The side panel: the same mirror beside every other view (never over the tmux view itself) */
// On every view change and side toggle: polls while the mirror is on screen anywhere, and draws what just appeared.
function syncTx() {
  if (view !== "tmux") tx.back = view;
  $("#txside").hidden = !(tx.side && view !== "tmux");
  document.body.classList.toggle("side-open", !$("#txside").hidden);
  fitPanels();
  $$("#btn-side, #tx-side").forEach((b) => b.setAttribute("aria-pressed", String(tx.side)));
  const shown = txShown();
  if (shown.length) startTx(); else stopTx();
  if (tx.loaded) shown.filter((k) => !tx.shown.has(k)).forEach((k) => renderTx(k));
  tx.shown = new Set(shown);
}
function setSide(on) {
  tx.side = on; saveSide(); syncTx();
  if (on && view === "tmux") location.hash = tx.back;  // the panel never shows over the tmux view: go where it does
}
function saveSide() {
  try { localStorage.setItem("kubesage-txside", JSON.stringify({open: tx.side, w: tx.sideW, win: tx.at.side.win})); } catch (e) { /* storage blocked */ }
}
function sizeSide(w) {
  tx.sideW = Math.round(Math.min(SIDE_MAX, Math.max(SIDE_MIN, w)));
  document.documentElement.style.setProperty("--side-w", `${tx.sideW}px`);
  $("#txs-grip").setAttribute("aria-valuemax", Math.max(SIDE_MIN, Math.floor(innerWidth * 0.6)));
  fitPanels();
}

function initTx() {
  try {
    const saved = JSON.parse(localStorage.getItem("kubesage-txside") || "{}");
    tx.side = saved.open === true;
    if (Number.isFinite(saved.w)) tx.sideW = saved.w;
    if (Number.isInteger(saved.win) && saved.win >= 0) tx.at.side.win = saved.win;
  } catch (e) { /* storage blocked */ }
  sizeSide(tx.sideW);
  $("#btn-side").innerHTML = `${icon("side")}<span>tmux panel</span>`;
  $("#btn-side").onclick = $("#tx-side").onclick = () => setSide(!tx.side);
  $("#txs-x").onclick = () => setSide(false);
  $("#txs-full").onclick = () => (location.hash = "tmux");
  for (const k of Object.keys(TX_EL)) {
    $(TX_EL[k].tabs).onclick = (ev) => {
      const b = ev.target.closest("[data-win]");
      if (!b) return;
      Object.assign(tx.at[k], {win: +b.dataset.win, zoom: null});
      if (k === "side") saveSide();
      renderTx(k);
    };
    $(TX_EL[k].box).onclick = (ev) => {
      const b = ev.target.closest("button[data-zoom]");
      if (!b) return;
      const at = tx.at[k];
      at.zoom = at.zoom === +b.dataset.zoom ? null : +b.dataset.zoom;
      txZoom(k); txTabs(k);
      // A pane hidden by a zoom comes back scrolled to the top; follow the newest lines as before.
      if (!tx.paused) $$(`${TX_EL[k].box} .tx-lines`).forEach((l) => (l.scrollTop = l.scrollHeight));
    };
  }
  const grip = $("#txs-grip");
  grip.onpointerdown = (ev) => { grip.setPointerCapture(ev.pointerId); document.body.classList.add("resizing"); };
  grip.onpointermove = (ev) => { if (grip.hasPointerCapture(ev.pointerId)) sizeSide(innerWidth - ev.clientX); };
  grip.onpointerup = grip.onpointercancel = () => { document.body.classList.remove("resizing"); saveSide(); };
  grip.onkeydown = (ev) => {
    const step = {ArrowLeft: 40, ArrowRight: -40}[ev.key];
    if (!step) return;
    ev.preventDefault();
    sizeSide(+grip.getAttribute("aria-valuenow") + step);
    saveSide();
  };
}
