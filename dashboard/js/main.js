"use strict";
// View switching and start-up. Loaded last: it calls into every other file.

const VIEWS = ["demo", "dashboard", "agents", "lab", "tmux"];
const PANELS = {demo: "#demo", dashboard: "#main", agents: "#agents", lab: "#lab", tmux: "#tmuxview"};
const TITLES = {demo: "KubeSage · Demo", dashboard: "KubeSage · FinOps", agents: "KubeSage · Agent team",
  lab: "KubeSage · Under the hood", tmux: "KubeSage · tmux"};
let view = null;

// A view name, or the id of anything inside a view (the FinOps section links), picks the view.
function viewFromHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  if (VIEWS.includes(id)) return id;
  return (id && document.getElementById(id)?.closest("[data-panel]")?.dataset.panel) || "demo";
}

function showView(next) {
  view = next;
  Object.entries(PANELS).forEach(([v, sel]) => ($(sel).hidden = v !== next));
  $$(".viewtab").forEach((t) => { const on = t.dataset.view === next; t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; });
  document.title = TITLES[next];
  $(".skip").href = PANELS[next];
  if (next === "lab") startLab(); else stopLab();
  syncTeam();
  syncTx();  // the tmux view, or the tmux panel beside this one
  if (next === "agents" && team.data) renderTeam();
  renderDemo();
  renderDock();
}

// The team's state feeds the Agent team view, the demo's caption bar and the agent chat beside every view.
function syncTeam() { if (view !== "dashboard" || demo.on || chat.open) startTeam(); else stopTeam(); }

function initViews() {
  $$(".viewtab").forEach((t) => {
    t.onclick = () => (location.hash = t.dataset.view);
    t.onkeydown = (ev) => {
      if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
      const tabs = [...$$(".viewtab")], next = tabs[(tabs.indexOf(t) + (ev.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      next.focus(); next.click();
    };
  });
  window.addEventListener("hashchange", () => { if (viewFromHash() !== view) showView(viewFromHash()); });
}

// The view keeps at least MAIN_MIN px between the panels beside it. The tmux panel narrows first, then the chat, each
// to its minimum; when even both minimums leave too little, the tmux panel floats over the view instead of beside it.
const MAIN_MIN = 560;
function fitPanels() {
  const sideOn = !$("#txside").hidden;
  let c = Math.min(chat.w, Math.floor(innerWidth * 0.34)), s = Math.min(tx.sideW, Math.floor(innerWidth * 0.6)), float = false;
  if (innerWidth > 900 && (chat.open ? c : 0) + (sideOn ? s : 0) + MAIN_MIN > innerWidth) {
    if ((chat.open ? CHAT_MIN : 0) + (sideOn ? SIDE_MIN : 0) + MAIN_MIN <= innerWidth) {
      let over = (chat.open ? c : 0) + (sideOn ? s : 0) + MAIN_MIN - innerWidth;
      if (sideOn) { const cut = Math.min(over, Math.max(0, s - SIDE_MIN)); s -= cut; over -= cut; }
      if (chat.open) c -= Math.min(over, Math.max(0, c - CHAT_MIN));
    } else {
      float = sideOn;
      c = Math.max(CHAT_MIN, Math.min(c, innerWidth - MAIN_MIN));
    }
  }
  document.documentElement.style.setProperty("--chat", `${c}px`);
  document.documentElement.style.setProperty("--side", `${s}px`);
  document.body.classList.toggle("side-over", float);
  $("#chat-grip").setAttribute("aria-valuenow", c);
  $("#txs-grip").setAttribute("aria-valuenow", s);
}
addEventListener("resize", fitPanels);

// The panels beside the views hang under the header, which takes more rows on a narrow screen.
new ResizeObserver(() => document.documentElement.style.setProperty("--top", `${$("header.top").offsetHeight}px`)).observe($("header.top"));

let laidOutAt = $("main").clientWidth;
new ResizeObserver(() => {
  const w = $("main").clientWidth;
  if (Math.abs(w - laidOutAt) < 4) return;
  laidOutAt = w;
  renderCluster();
  if (report) { renderBars(); renderFlow(false); }
}).observe($("main"));

(async () => {
  try {
    config = await api("/api/config");
    const live = config.mode !== "mock";
    $("#mode").innerHTML = `<span class="pulse-dot" style="--c:${live ? "var(--good)" : "var(--warn)"}"></span>${live ? "Live" : "Mock"}<span class="mode-llm"> LLM</span>`;
    $("#mode").title = live ? "Live LLM: the agents call a real model" : "Mock LLM: deterministic stand-ins, no model calls";
    $("#btn-gitops").hidden = !config.gitops;
  } catch (e) { $("#mode").textContent = "Server unavailable"; log(e.message, "err"); notice("Cannot load server configuration. Check the KubeSage server connection.", "err"); }
  try {
    const res = await api("/api/report");
    runId = res.run; report = res.report;
  } catch (e) { if (e.status !== 404) log(e.message, "err"); }
  try { await refreshReports(); } catch (e) { log(e.message, "err"); }
  renderReport(true);
  wireChanged();  // a page opened on Under the hood shows the latest run's trace now, not at the next poll
  await refreshCluster();
  setInterval(refreshCluster, 3000);
  setInterval(renderSteps, 1000);
})();
initLab();
initTeam();
initDemo();
initTx();
initChat();
initViews();
showView(viewFromHash());
