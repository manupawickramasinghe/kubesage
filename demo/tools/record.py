"""Record the KubeSage demo video against the replay server. Slides first (title, the summary, the hypothetical cluster
and how KubeSage works), then one minute of the real dashboard on that cluster in the slides' #app frame, then a
closing slide. Chrome's own screencast frames (1600x900) are saved while each scene is held for its voice-over; each
slide's parts appear sentence by sentence. Writes frames/, frames.json (frame timestamps) and scenes.json (scene start
times, for the audio and subtitles)."""
import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
WORK = Path(os.environ.get("KUBESAGE_VIDEO_WORK", HERE / "build"))  # frames, audio and timings
sys.path.insert(0, str(HERE))
import sim_server  # noqa: E402

FRAMES = WORK / "frames"
DUR = {s["id"]: s["seconds"] for s in json.loads((WORK / "audio" / "durations.json").read_text())}
SAY = {s["id"]: s["narration"] for s in json.loads((HERE / "narration.json").read_text())}
URL = "http://127.0.0.1:8700/"
LEAD = 0.4       # the voice starts this long after its scene (build.py)
PAD = 0.6        # silence after a slide's voice-over
DEMO_PAD = 0.15  # and after each step of the one-minute demo
DEMO_MAX = 60.0

# The dashboard's Demo mode, cut to the six steps the minute shows, with captions for the hypothetical cluster.
STEPS = """() => {
  const keep = [[0, "The cluster", "shop-prod, a hypothetical online store on two Azure nodes. Each glass is the CPU a pod "
      + "reserves; the block inside is what it uses."],
    [1, "Find the waste", "Free statistics first, one cheap triage call, and more reasoning only for the harder workloads."],
    [3, "Apply the plan", "Right-sized requests, the KEDA scaling rule, and the cheaper F8s_v2 node."],
    [4, "A flash sale", "The recorded 60 req/s load test, replayed at SPEED× speed: CPU passes its 70% target and KEDA scales "
      + "checkout-web out."],
    [5, "A bad release", "ledger-cache needs about 700 MiB under a 512 MiB limit, so it is OOM-killed. Alertmanager fires an alert."],
    [6, "The team investigates", "Four small agents gather evidence at once; the Thinker finds the root cause."],
    [7, "Ship the fix", "The Executor writes the fix as a git commit. A person decides whether it ships."]];
  const steps = keep.map(([i, title, say]) => Object.assign(DEMO[i], {title, say}));
  DEMO.splice(0, DEMO.length, ...steps);
  buildDemo();  // the Demo tab's list of steps, which is hidden behind the slides
  setChat(false);
}"""
# Where the dashboard speaks of the live lab, say what this is instead.
SCRUB = """() => {
  const n = [...document.querySelectorAll("#main .notice p")].find((p) => p.textContent.startsWith("Demo data"));
  if (n) n.innerHTML = "<b>Simulation:</b> a hypothetical cluster. The analysis and the agent run are KubeSage's recorded "
    + "output, shown on its nodes. Dollar amounts use Azure list prices; they are potential savings, not a measured bill reduction.";
  $("#snapshot-note").textContent = "Savings and recommendations are estimates from an analysis. The cluster below is simulated.";
  $("#cluster-card h2").textContent = "Cluster (simulated)";
  $("#agents .lab-hero p").innerHTML = "Start a test case to cause a problem on the cluster. Four sub-agents on a small model "
    + "gather evidence, the Thinker on a large model works out the cause, and the Executor writes the fix as a commit under "
    + "<code>gitops/</code>. Agents, models and rules live in the repo's <code>agents/</code> folder.";
  $("#mode").innerHTML = '<span class="pulse-dot" style="--c:var(--warn)"></span>Simulation · hypothetical cluster';
  $("#mode").title = "A simulation: recorded KubeSage output on a hypothetical cluster";
  $("#btn-reset").style.display = "none";  // not part of this demo, and the badge needs its room
  const swap = (root) => {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) if (n.nodeValue.includes("Live metrics")) n.nodeValue = n.nodeValue.replace("Live metrics", "Simulated metrics");
  };
  swap(document.body);
  new MutationObserver((ms) => ms.forEach((m) => {
    if (m.type === "characterData") { if (m.target.nodeValue.includes("Live metrics")) m.target.nodeValue = m.target.nodeValue.replace("Live metrics", "Simulated metrics"); }
    else m.addedNodes.forEach((x) => { if (x.nodeType === 1) swap(x); else if (x.nodeType === 3 && x.parentNode) swap(x.parentNode); });
  })).observe(document.body, {childList: true, subtree: true, characterData: true});
}"""


def sentences(text):
    return [x for x in re.split(r"(?<=[.!?])\s+", text) if x]


def cue_times(sid, start):
    """When each sentence of a scene's voice-over starts (by its share of the characters), then its end."""
    parts = sentences(SAY[sid])
    total = sum(len(x) for x in parts)
    t, out = start + LEAD, []
    for x in parts:
        out.append(t)
        t += DUR[sid] * len(x) / total
    return out + [start + LEAD + DUR[sid] - 0.6]


def main():
    shutil.rmtree(FRAMES, ignore_errors=True)
    FRAMES.mkdir(parents=True)
    server = sim_server.serve(8700)
    frames, scenes, marks = [], [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900}, color_scheme="light", reduced_motion="no-preference")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(URL + "demo/tools/slides/index.html")
        page.wait_for_function("document.fonts.status === 'loaded'")
        page.evaluate("() => { document.getElementById('app').src = '/'; }")
        page.wait_for_function("() => { const f = document.getElementById('app').contentWindow; "
                               "return f && f.document.readyState === 'complete' && f.document.fonts.status === 'loaded' && typeof f.goStep === 'function'; }")
        app = page.frame(name="app")
        app.wait_for_function("() => document.querySelector('#mode').textContent.includes('LLM')")
        app.evaluate(STEPS.replace("SPEED", f"{sim_server.SPEED:g}"))
        app.evaluate(SCRUB)
        fr = page.frame_locator("#app")
        page.evaluate("show('s-title')")
        page.wait_for_timeout(1200)

        cdp = ctx.new_cdp_session(page)

        def on_frame(ev):
            name = f"{len(frames):06d}.jpg"
            (FRAMES / name).write_bytes(base64.b64decode(ev["data"]))
            frames.append([ev["metadata"]["timestamp"], name])
            cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1600, "maxHeight": 900, "everyNthFrame": 1})
        page.wait_for_timeout(600)
        start = time.time()

        def scene(sid, pad=PAD, hold=0):
            now = time.time()
            scenes.append({"id": sid, "t": now})
            return now, now + max(LEAD + DUR[sid] + pad, hold)

        def until(t, cues=()):
            cues = sorted(cues)
            while time.time() < t:
                while cues and time.time() >= cues[0][0]:
                    cues.pop(0)[1]()
                page.wait_for_timeout(min(100, max(10, (t - time.time()) * 1000)))

        def mark(name):
            marks[name] = time.time()

        def slide(sid):
            page.evaluate(f"show('s-{sid}')")
            t0, end = scene(sid)
            until(end, [(t, (lambda k=k: page.evaluate(f"cue({k})"))) for k, t in enumerate(cue_times(sid, t0))])
            mark(sid)

        nxt = lambda: fr.locator("#dk-next").click()
        act = lambda: fr.locator("#dk-actions [data-run]").first.click()

        _, end = scene("title")
        until(end)
        for sid in ("summary", "server", "rush", "finops", "team"):
            slide(sid)

        # The website: one minute on the hypothetical cluster.
        app.evaluate("() => goStep(0)")
        page.wait_for_timeout(700)
        demo_at = time.time()
        page.evaluate("app(true)")
        t0, end = scene("d-analyze", DEMO_PAD)               # The cluster, then Find the waste
        app.evaluate("() => $('#btn-analyze').click()")
        page.wait_for_timeout(2200)
        mark("demo-cluster")
        until(end, [(cue_times("d-analyze", t0)[2], nxt)])  # as the voice reaches Analyze
        mark("demo-analysis")

        nxt()                                              # Apply the plan
        _, end = scene("d-apply", DEMO_PAD, hold=7.0)
        page.wait_for_timeout(300)
        act()
        page.wait_for_timeout(6500)
        mark("demo-applied")
        until(end)

        nxt()                                              # A flash sale
        _, end = scene("d-flash", DEMO_PAD, hold=sim_server.TRAFFIC_S - 0.5)
        page.wait_for_timeout(300)
        act()
        page.wait_for_timeout(5500)
        mark("demo-flash")
        until(end - 2.5)
        # The bad release ships as the sale winds down, so the team has its commit by the time the minute ends.
        app.evaluate("() => { $('#tc-select').value = 'memory';"
                     " Promise.resolve(tcAction('start', 'memory')).then(() => { $('#toast').hidden = true; }); }")  # no pop-up
        until(end)

        nxt()                                              # A bad release
        _, end = scene("d-incident", DEMO_PAD)
        app.evaluate("() => setChat(true)")
        until(end)
        mark("demo-incident")

        nxt()                                              # The team investigates: held until the Executor has its commit
        _, end = scene("d-team", DEMO_PAD)
        page.wait_for_timeout(int(DUR["d-team"] * 600))
        mark("demo-team")
        until(end)
        fr.locator("#team-flow .agent[data-state='done']").nth(5).wait_for(timeout=20000)

        nxt()                                              # Ship the fix
        _, end = scene("d-fix", DEMO_PAD)
        fr.locator("#dk-min").click()                      # the caption bar folds away from the diff
        page.wait_for_timeout(1000)
        app.evaluate("() => [...document.querySelectorAll('#team-detail .det-h')].find((h) => h.textContent.startsWith('Change'))"
                     ".scrollIntoView({behavior: 'smooth', block: 'start'})")  # the commit and its diff
        page.wait_for_timeout(2500)
        mark("demo-fix")
        until(end)
        demo_s = time.time() - demo_at

        page.evaluate("app(false)")
        page.evaluate("show('s-close')")
        _, end = scene("close", pad=1.8)
        page.wait_for_timeout(2500)
        mark("close")
        until(end)
        cdp.send("Page.stopScreencast")
        page.wait_for_timeout(300)
        stop = time.time()
        browser.close()
    server.shutdown()
    (WORK / "frames.json").write_text(json.dumps({"frames": frames, "start": start, "stop": stop}))
    (WORK / "scenes.json").write_text(json.dumps({"scenes": scenes, "marks": marks, "start": start, "stop": stop}, indent=1))
    print(f"{len(frames)} frames, {stop - start:.1f} s, website {demo_s:.1f} s, page errors: {errors}")
    if demo_s > DEMO_MAX:
        print(f"warning: the website part ran {demo_s:.1f} s, over {DEMO_MAX:.0f} s")


if __name__ == "__main__":
    main()
