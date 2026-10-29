"""Record the KubeSage demo against the replay server: Chrome's own screencast frames (1920x1080) while the app's
Demo mode walks through its nine steps, each scene held for its voice-over. Writes frames/, frames.json (frame
timestamps) and scenes.json (scene start times, for the audio and subtitles)."""
import base64
import json
import os
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
URL = "http://127.0.0.1:8700/"
PAD = 1.2  # silence after each scene's voice-over

OVERLAY_CSS = """
#vx-card { position: fixed; inset: 0; z-index: 9999; display: grid; place-items: center; text-align: center;
  background: radial-gradient(1200px 600px at 50% 40%, color-mix(in srgb, var(--rec) 14%, var(--bg)), var(--bg));
  transition: opacity .9s ease; font-family: var(--sans); color: var(--fg); }
#vx-card .in { display: grid; gap: 14px; justify-items: center; max-width: 980px; padding: 0 40px; }
#vx-card svg { width: 84px; height: 84px; stroke: var(--rec); }
#vx-card h1 { font: 400 92px/1 var(--serif); letter-spacing: -.02em; margin: 6px 0 0; }
#vx-card .vx-tag { font-size: 26px; color: var(--fg-2); }
#vx-card .sub { margin-top: 18px; font: 500 15px var(--mono); letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
#vx-card .url { margin-top: 10px; font: 500 22px var(--mono); color: var(--fg); }
#vx-chip { position: fixed; z-index: 9998; top: 17px; left: 12px;
  display: inline-flex; align-items: center; gap: 6px; padding: 4px 9px; border-radius: 99px; border: 1px solid var(--border);
  background: var(--card); box-shadow: var(--shadow); font: 600 10.5px var(--mono); letter-spacing: .04em; color: var(--fg-2);
  text-transform: uppercase; pointer-events: none; }
#vx-chip i { width: 7px; height: 7px; border-radius: 50%; background: var(--rec); }
"""
LOGO = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="1.4" stroke-linejoin="round"><path d="M12 2.5 20.5 7.4v9.2L12 '
        '21.5l-8.5-4.9V7.4z"/><path d="M12 12 20.5 7.4M12 12v9.5M12 12 3.5 7.4"/></svg>')


def card(page, html):
    page.evaluate("""html => { let c = document.getElementById('vx-card');
      if (!c) { c = document.createElement('div'); c.id = 'vx-card'; document.body.append(c); }
      c.innerHTML = '<div class="in">' + html + '</div>'; c.style.opacity = 1; }""", html)


def card_out(page):
    page.evaluate("() => { const c = document.getElementById('vx-card'); if (c) { c.style.opacity = 0; setTimeout(() => c.remove(), 1000); } }")


def chip(page, text):
    page.evaluate("""t => { let c = document.getElementById('vx-chip');
      if (!c) { c = document.createElement('div'); c.id = 'vx-chip'; document.body.append(c); }
      c.innerHTML = '<i></i>' + t; }""", text)


def main():
    shutil.rmtree(FRAMES, ignore_errors=True)
    FRAMES.mkdir(parents=True)
    server = sim_server.serve(8700)
    sim = sim_server.SIM
    frames, scenes, marks = [], [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900}, device_scale_factor=1.2, color_scheme="light",
                                  reduced_motion="no-preference")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(URL + "#demo")
        page.wait_for_function("document.fonts.status === 'loaded'")
        page.add_style_tag(content=OVERLAY_CSS)
        # The first caption says "This is not a simulation." True live; this video is a replay of recorded runs.
        page.evaluate("() => { DEMO[0].say = DEMO[0].say.replace('This is not a simulation. ', ''); }")
        card(page, f"""{LOGO}<h1>KubeSage</h1><div class="vx-tag">An AI SRE team for Kubernetes</div>
          <div class="sub">A replay of real runs recorded on a two-machine cluster · 29 October 2026</div>""")
        page.wait_for_timeout(1500)

        cdp = ctx.new_cdp_session(page)

        def on_frame(ev):
            name = f"{len(frames):06d}.jpg"
            (FRAMES / name).write_bytes(base64.b64decode(ev["data"]))
            frames.append([ev["metadata"]["timestamp"], name])
            cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1920, "maxHeight": 1080, "everyNthFrame": 1})
        page.wait_for_timeout(600)
        start = time.time()

        def scene(sid, hold=None):
            scenes.append({"id": sid, "t": time.time()})
            return time.time() + max(DUR[sid] + PAD, hold or 0)

        def until(t):
            while time.time() < t:
                page.wait_for_timeout(min(250, max(10, (t - time.time()) * 1000)))

        def mark(name):
            marks[name] = time.time()

        nxt = lambda: page.locator("#dk-next").click()
        act = lambda: page.locator("#dk-actions [data-run]").first.click()

        end = scene("title")
        until(end - 0.8)
        card_out(page)
        page.wait_for_timeout(900)

        end = scene("pitch")
        chip(page, "Replay")
        page.wait_for_timeout(1500)
        mark("demo")
        until(end)

        page.locator("#demo-start").click()               # 1 · A real cluster
        end = scene("cluster")
        page.wait_for_timeout(2500)
        mark("cluster-before")
        until(end)

        nxt()                                             # 2 · Find the waste
        end = scene("analyze", hold=10)
        page.wait_for_timeout(900)
        act()
        page.locator("#dk-status").filter(has_text="workloads change").wait_for(timeout=20000)
        page.wait_for_timeout(1500)
        mark("analysis")
        until(end)

        nxt()                                             # 3 · How it decided
        end = scene("decided")
        page.wait_for_timeout(3000)
        mark("decided")
        until(end)

        nxt()                                             # 4 · Apply the plan
        end = scene("apply", hold=12)
        page.wait_for_timeout(1200)
        act()
        page.wait_for_timeout(9000)
        mark("cluster-after")
        until(end)

        nxt()                                             # 5 · Prove it still scales
        end = scene("traffic", hold=sim_server.TRAFFIC_S + 6)
        page.wait_for_timeout(800)
        act()
        chip(page, f"Replay · {sim_server.SPEED:g}× speed")
        page.wait_for_timeout(13000)
        mark("traffic-scaling")
        page.wait_for_timeout(sim_server.TRAFFIC_S * 1000 - 9000)
        mark("traffic-result")
        until(end)
        chip(page, "Replay")

        nxt()                                             # 6 · Break something
        end = scene("break", hold=14)
        page.wait_for_timeout(800)
        page.locator("#dk-tc").select_option("memory")
        page.wait_for_timeout(400)
        act()
        page.locator("#tc-live").filter(has_text="ContainerOOMKilled").wait_for(timeout=20000)
        sim.pending_until = max(time.time() + 6, end)     # the real team waits 45 s for related alerts; shortened here
        page.wait_for_timeout(2500)
        mark("incident")
        until(end)

        nxt()                                             # 7 · The team investigates
        end = scene("investigate", hold=22)
        page.locator("#btn-side").click()
        page.wait_for_timeout(9000)
        mark("team-running")
        page.locator("#team-flow .agent[data-state='done']").nth(5).wait_for(timeout=30000)
        page.wait_for_timeout(1500)
        mark("team-done")
        until(end)

        page.locator("#txs-x").click()
        nxt()                                             # 8 · Ship the fix
        end = scene("ship")
        page.wait_for_timeout(int(DUR["ship"] * 450))
        page.locator("#team-detail [data-exec='dry']").click()
        page.wait_for_timeout(2500)
        mark("fix")
        until(end)

        nxt()                                             # 9 · Why it matters
        end = scene("why")
        page.wait_for_timeout(2500)
        mark("why")
        until(end)

        scenes.append({"id": "outro", "t": time.time()})
        card(page, f"""{LOGO}<h1>KubeSage</h1><div class="vx-tag">Find the waste. Fix what breaks. A person decides.</div>
          <div class="url">github.com/manupawickramasinghe/kubesage</div>""")
        page.wait_for_timeout(6000)
        cdp.send("Page.stopScreencast")
        page.wait_for_timeout(300)
        stop = time.time()
        browser.close()
    server.shutdown()
    (WORK / "frames.json").write_text(json.dumps({"frames": frames, "start": start, "stop": stop}))
    (WORK / "scenes.json").write_text(json.dumps({"scenes": scenes, "marks": marks, "start": start, "stop": stop}, indent=1))
    print(f"{len(frames)} frames, {stop - start:.1f} s, page errors: {errors}")


if __name__ == "__main__":
    main()
