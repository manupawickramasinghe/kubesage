# KubeSage demo

Everything you can watch or look at: the narrated demo video, the pictures and slides made for it, every screenshot in
the docs, and the tools that rebuild the video.

<a href="video/kubesage-demo.mp4"><img src="video/poster.png" alt="Play the KubeSage demo video: the title slide, KubeSage, an AI SRE team for Kubernetes" width="100%"></a>

**[▶ Watch the demo video](video/kubesage-demo.mp4)**: 2 min 55 s, 1600×900, narrated (Kokoro voice), with English subtitles
([`kubesage-demo.srt`](video/kubesage-demo.srt)).

## What the video shows

Slides first: the summary of what is new, a hypothetical customer cluster, event-driven scaling for sale-day rushes,
and how KubeSage finds waste and fixes incidents. Then 55 seconds of the real dashboard on that cluster, and a closing
slide. The website does not appear until the summary has been shown.

| time | chapter | what happens |
|---|---|---|
| 0:00 | Title | KubeSage, an AI SRE team for Kubernetes |
| 0:08 | What's new | Five points: FinOps and SRE in one system, built for the rush, cents not dollars, evidence or it's dropped, a person decides |
| 0:30 | The scenario | *shop-prod*, a hypothetical online store on Azure Kubernetes: a D8s_v5 node running five services, a cheaper F8s_v2 node standing by, a weekday-morning rush, and a ledger cache about to ship with too little memory |
| 0:50 | Built for the rush | An illustrative week of checkout traffic: KEDA adds pods before the weekday rush KubeSage learned, a CPU trigger catches a flash sale, and engineers add a Black Friday window to the same rule |
| 1:14 | Finding the waste, for a cent | The FinOps pipeline: free statistics, one triage call, more reasoning only for hard cases, guardrails, the plan ($0.0111 for the whole analysis) |
| 1:31 | How the team fixes incidents | An alert, four sub-agents, the evidence check, the Thinker and your rules, the Executor, the policy check, the commit, a person |
| 1:51 | **The website, under a minute** | The cluster · Analyze ($33.58/mo for $0.0111) · Apply · a flash sale (KEDA scales checkout-web to six pods) · the bad release (`ContainerOOMKilled`) · the agent team, with its chat · the fix as a commit and its diff (16 s, $0.02) |
| 2:47 | Close | The repository |

## What is real, and what is hypothetical

- **Hypothetical:** the customer. *shop-prod* and its online store are a scenario for this video, and its two nodes are
  shown as the Azure VMs they would be: `aks-system-0` (D8s_v5) and `aks-lab-0` (F8s_v2).
- **Recorded, unchanged:** what KubeSage did. The pods, their usage and the FinOps analysis (run `20261029-114842-131212`,
  with its model-call trace) and the agent-team run `20261029-034845` (every agent's report, the diagnosis, the commit and
  its diff, the time and the cost) were recorded on the project's two-machine lab cluster, whose machines the cost model
  already prices as those two VMs. [tools/scenario.py](tools/scenario.py) renames the nodes, describes their hardware as
  the VMs', rewrites the two platform rules that described the lab machines, and leaves out four lines of an agent's
  reasoning about the lab hardware. Nothing else is changed.
- **Simulated:** the website part is the real dashboard answered by a small local server
  ([tools/sim_server.py](tools/sim_server.py)) with that recorded data. Nothing was sent to a cluster and no model was
  called while recording. The dashboard's header says "Simulation · hypothetical cluster" throughout.
- **Shortened:** Apply's rollout plays twice as fast as recorded. The flash sale is the recorded 60 req/s load test of
  27 October ([docs/testing.md](../docs/testing.md)) replayed at 20× speed, and the dashboard's caption says so. The
  alert fires half a second after the test case starts and the team starts 0.3 s later: on a cluster the alert takes
  about a minute and the team then waits 45 s for related alerts. The team run itself plays at its recorded pace.
- **Illustrative:** the week of traffic on the "Built for the rush" slide is drawn to explain the idea. Its 08:50–11:10
  weekday window, 70% CPU target and 3 to 6 pods are the recorded plan's. KubeSage learns weekly peaks; it does not
  read a sales calendar, so the Black Friday window is shown as one your engineers add to the KEDA rule it writes.

The voice-over was drafted by Claude, edited for clarity by **agy** (Gemini 4 Argon High) and fact-checked read-only
against the repository by **Codex**, whose corrections are in it: [tools/narration-review.md](tools/narration-review.md).

## Pictures and slides

The six pictures in [`images/generated/`](images/generated) were made for this video by agy's image generator, in the
dashboard's palette, with no text in them: the title (`hero.jpg`), the store and its servers (`store.jpg`), a sale-day
rush (`rush.jpg`), the incident (`incident.jpg`), the agent team (`team.jpg`) and a person reviewing a change
(`review.jpg`). The slides are one
HTML page, [`tools/slides/index.html`](tools/slides/index.html), with the diagrams drawn in SVG; each slide's parts
appear as the voice-over reaches them.

## Folder

| path | what it holds |
|---|---|
| [`video/`](video) | `kubesage-demo.mp4`, its subtitles `kubesage-demo.srt`, and `poster.png` |
| [`images/`](images) | the logo, and the generated pictures in `generated/` |
| [`screenshots/`](screenshots) | every screenshot the README and the docs use (`ui-*`, `readme-*`), taken from the live lab cluster |
| [`screenshots/simulation/`](screenshots/simulation) | 14 stills from the video, one per slide and per website step, at full resolution |
| [`tools/`](tools) | the slides, the replay server and its scenario, the recorder, the voice-over and the build script, with the recorded data |

## Rebuild the video

```bash
.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium   # once
demo/tools/make_video.sh                  # ~4 minutes: voice-over, recording, encoding
```

`make_video.sh` installs [Kokoro](https://github.com/thewh1teagle/kokoro-onnx), an open text-to-speech model that
runs locally (voice `af_heart`), into `demo/tools/build/`, which git ignores. It then synthesises
[`tools/narration.json`](tools/narration.json), records the slides and the dashboard against
[`tools/sim_server.py`](tools/sim_server.py) with Chrome's screencast, and encodes H.264 with the voice and subtitles.
To change what is said, edit `narration.json`: each scene is held for as long as its narration takes, and a slide
reveals one part per sentence. The recorder warns if the website part runs over a minute; keep the whole video under three. To record against new data,
replace the files in [`tools/data/`](tools/data) with fresh GETs from a running Control Center (`/api/cluster`,
`/api/report`, `/api/agents`, …). They hold no secrets: the trace shows the API key only as `$CEREBRAS_API_KEY`.
