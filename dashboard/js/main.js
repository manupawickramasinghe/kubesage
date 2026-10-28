"use strict";
// View switching and start-up. Loaded last: it calls into every other file.

const VIEWS = ["dashboard", "lab", "tmux"];
const viewFromHash = () => (VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "dashboard");
function showView(view) {
  $("#main").hidden = view !== "dashboard";
  $("#lab").hidden = view !== "lab";
  $("#tmuxview").hidden = view !== "tmux";
  $$(".viewtab").forEach((t) => { const on = t.dataset.view === view; t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; });
  document.title = {dashboard: "KubeSage · AI Control Center", lab: "KubeSage · Architecture & CLI lab", tmux: "KubeSage · tmux"}[view];
  if (view === "lab") startLab(); else stopLab();
  if (view !== "dashboard") startTeam(); else stopTeam();
  if (view === "tmux") startTx(); else stopTx();
  if (view === "lab" && team.data) renderTeam();
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
showView(viewFromHash());
