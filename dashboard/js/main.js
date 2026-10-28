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
  if (next !== "dashboard" || demo.on) startTeam(); else stopTeam();  // the demo's caption bar shows the team's state
  if (next === "tmux") startTx(); else stopTx();
  if (next === "agents" && team.data) renderTeam();
  renderDemo();
  renderDock();
}

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
    $("#mode").innerHTML = `<span class="pulse-dot" style="--c:${live ? "var(--good)" : "var(--warn)"}"></span>${live ? "Live LLM" : "Mock LLM"}`;
    $("#btn-gitops").hidden = !config.gitops;
  } catch (e) { $("#mode").textContent = "Server unavailable"; log(e.message, "err"); notice("Cannot load server configuration. Check the KubeSage server connection.", "err"); }
  try {
    const res = await api("/api/report");
    runId = res.run; report = res.report;
  } catch (e) { if (e.status !== 404) log(e.message, "err"); }
  try { await refreshReports(); } catch (e) { log(e.message, "err"); }
  renderReport(true);
  await refreshCluster();
  setInterval(refreshCluster, 3000);
  setInterval(renderSteps, 1000);
})();
initLab();
initTeam();
initDemo();
initViews();
showView(viewFromHash());
