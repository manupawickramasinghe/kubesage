# Handover: UI/UX review and testing of KubeSage

You are taking over **KubeSage**, an AI FinOps agent for Kubernetes built for a hackathon. Your job is to
**review the web UI's UX and visual design, and test the product end to end** on the real two-machine
cluster. Fix what you find through pull requests.

Snapshot taken 2026-10-27 (Asia/Colombo). Repo: `~/kubesage` on the 8600G desktop, GitHub
`manupawickramasinghe/kubesage` (**public**), default branch `main`. Every earlier PR (#1–#8) is merged.

## Rules you must follow

1. **Git identity.** Commit only as `M.N Wickramasinghe <manupawickramasinghe@users.noreply.github.com>`, the
   user's account; it is already the repo's git config. **No `Co-Authored-By` trailers. No "Generated with …"
   lines in commits or PR bodies. Never add an AI or bot as a collaborator.**
2. **Secrets.** API keys live only in `~/kubesage/.env` (gitignored, mode 600). Never print, log, commit or
   paste a key. The repo is public, so check `git grep` before pushing. The laptop's sudo password is not
   stored anywhere; ask the user if you need it.
3. **Workflow.** Branch from `main`, open a PR, describe what you verified. Don't push to `main` directly.
   Merge only when the user asks.
4. **Shared machines.** Both machines run other important services:
   - the **desktop** runs Frigate and other containers; don't stop or modify any container that isn't `k3d-kubesage-*` or `kubesage-*`;
   - the **laptop** runs Pi-hole (LAN DNS), GitHub runners and more; don't touch them.
   - Ask before anything destructive: `cluster-down.sh`, `leave-node.sh`, uninstalls, reboots.
5. **Off-limits elsewhere** (the user's standing rules):
   - repositories of the `Rikugan` / `Rikugan-LK` organisations;
   - `esp32-blue-audio-puck`;
   - branch `test/upstream-0.18-rust-integration` in `Zektopic/frigate`.
6. **Costs.** Each live **Analyze** spends ~$0.01 of the user's Cerebras credit. **Send traffic** burns about
   4 real cores on the laptop for its duration. Both are fine for testing; don't loop them.

## What exists

```
browser ──▶ kubesage serve (desktop :8600) ──▶ VictoriaMetrics (desktop :8428) ◀── vmagent (in cluster)
                     │                     └──▶ LLM provider (Cerebras, gpt-oss-120b)
                     └──▶ Kubernetes API 192.168.1.9:6443
                            ├─ node k3d-kubesage-server-0  = 8600G desktop "pop-os", control plane (k3s in k3d)
                            └─ node manupa-hnx             = 3500U laptop, worker (native k3s agent)
```

Read these first, about 20 minutes:

- [`ui-guide.md`](ui-guide.md): what every part of the UI means and the 3-step demo. **Your baseline for UX
  review.**
- [`cluster.md`](cluster.md): the two machines, networking and its traps, what was installed where.
- [`architecture.md`](architecture.md), [`pipeline.md`](pipeline.md), [`api.md`](api.md),
  [`operations.md`](operations.md), [`testing.md`](testing.md), [`configuration.md`](configuration.md).

## Run it

```bash
cd ~/kubesage
export KUBECONFIG=$PWD/.bin/kubeconfig PATH=$PWD/.bin:$PATH
kubectl get nodes -L kubesage.io/sku -o wide        # both Ready?
set -a; . ./.env; set +a
KUBESAGE_MODELS=cerebras .venv/bin/kubesage serve --host 0.0.0.0
# UI: http://192.168.1.9:8600  ·  Grafana: http://localhost:3300
.venv/bin/python -m pytest -q                       # 21 tests, ~3 s
```

A `kubesage serve` may already be running in the background on :8600 from the previous session. Check with
`ss -ltnp | grep 8600` and stop it before starting your own. If you change `index.html` or `server.py`, restart
the server: the page is read once at startup.

The cluster was left in the **"before"** state (Reset demo): every workload on the desktop with oversized
requests. The latest analysis run in `out/` is from the live model.

## The UI in code

Everything is in **one file**, `kubesage/web/static/index.html`: HTML, CSS, one `<script>`, and inline SVG. The
server is `kubesage/web/server.py`.

Hard constraints:

- **No CDN and no build step.** Fonts are vendored in `kubesage/web/static/fonts`: Inter for text and Instrument
  Serif for headings, both SIL OFL. The user chose them from ascender.club, so keep them.
- **Exactly one `<script>` tag.** `tests/test_web.py` extracts it and runs `node --check`.
- Every POST needs the `X-KubeSage: 1` header. Use the `api()` helper.
- **Escaping:** data goes into `innerHTML` only through `esc()`, and tooltips are built with `textContent`.
  Pod, node and model text is untrusted.
- Light and dark tokens live on `:root`, with dark under `prefers-color-scheme` and `[data-theme="dark"]`.
  Workload colours `--s1…--s8` come in a fixed order checked for colour-blind separation. Status colours
  (`--good/--warn/--crit`) always come with an icon and a label. `--rec` (violet) means "recommended" and
  "machine CPU".
- Every animation must mean a real event: a pod created, removed or moved, or a new usage reading. The
  headline sweep and the busy progress bar are the only decorative motion, and the user asked for both. Honour
  `prefers-reduced-motion`.

Main functions:

| function | renders |
|---|---|
| `renderSteps` | the ①②③ strip and its status lines |
| `renderFigures` | the savings strip |
| `renderCluster` | the isometric scene (`prism`/`project` geometry; `tweenCores` eases pod cores to new readings) |
| `renderLegend`, `renderInspector`, `nodePanel`, `appPanel` | chips and the right-hand panel |
| `renderBars` / `bullet` | the Reserved vs used bullet charts, drawn at the container's pixel width |
| `renderFlow` | the router flow and the LLM ledger |
| `renderWorkloads` | the recommendation rows |
| `select`, `applyFocus`, `bindTips` | cross-diagram selection, highlighting, tooltips, keyboard |

## UX review: where to look first

The user said they **didn't understand the UI** before the step strip and `ui-guide.md` existed. Judge
everything by "would a hackathon judge get this in 10 seconds?"

1. **First impression.** Is it obvious what to press and what the page proves? Is the hero headline earning
   its space, or should the savings lead?
2. **Report vs live mismatch.** The savings strip, charts and recommendation rows come from the *last
   analysis*. The cluster drawing is *live*. After Apply, "Reserved now" in the charts still shows the old
   requests. Is that confusing? Should it be labelled ("from run …") or partly live?
3. **Cluster drawing.**
   - Can people read glass vs core without the key?
   - Is 46 px per core right?
   - Do labels collide with 3+ machines or 12+ pods on one machine?
   - Is the column layout (container under 560 px) right on tablets?
   - Is the 10-light CPU strip readable?
   - The desktop's usage is only the k3d container's share. Is that caveat clear enough?
4. **Inspector discoverability.** Do people find out they can click? The hint is in the card description only.
5. **Steps.**
   - Step 2's "Applied" status only lives in the browser session; a reload loses it. Could it be derived from the
     cluster instead, e.g. "pods are on the F8s_v2 machine"?
   - Is Dry run understood?
6. **Send traffic.** It only adds pods if checkout-web is below its max (3 in the latest plan) and the load
   crosses the 70% target, so 60 req/s is the default (80 overloaded a 4-replica plan). Is that understandable on screen, or should the UI show
   the autoscaler target and current CPU next to step 3?
7. **Numbers that look wrong.** In some runs a workload's monthly cost *rises* after the change, because the
   guardrails add a replica or CPU. Is that explained well enough by the guardrail notes?
8. **Accessibility.**
   - The light-mode aqua, yellow and magenta workload colours are below 3:1 contrast, so they rely on text
     labels.
   - Check that keyboard focus is visible in the SVGs.
   - Check that screen-reader labels make sense.
   - Check reduced motion.
9. **Mobile.** 390 px: steps, charts and the stacked cluster. See `docs/images/ui-phone.png`.
10. **Copy.** Terse, plain-English labels. Check consistency: "reserve/reserved/request", "machine/node".

## Testing: what to verify

- **Automated:** `pytest -q` is green. Add tests for anything you change in `server.py` or the pipeline.
- **End to end on the real cluster:** follow "the demo flow end to end" in [`testing.md`](testing.md):
  1. Reset;
  2. Analyze: the header shows Live LLM;
  3. Apply: pods move to `manupa-hnx`;
  4. Send traffic: CPU rises, pods are added if below max;
  5. Reset again at the end.
- **Cross-machine networking:** the `nettest` pod in [`testing.md`](testing.md).
- **Browsers:** Firefox (or Zen) and Chromium, light and dark, 390 / 768 / 1400 px widths.
- **Failure states:** stop VictoriaMetrics briefly (`docker stop kubesage-victoriametrics-1`, then start it
  again) and confirm Analyze shows a readable error. The UI must say "Cluster not reachable" when the API
  is down. Don't actually take the cluster down without asking.

## Known issues and limits

- The **30-day history is synthetic** (`kubesage simulate`) plus a live tail. Machines, pods, usage, scaling and
  LLM costs are real. Prices are a cost model, not the hardware's price.
- The LLM sometimes proposes **scale-to-zero for checkout-web** and **cron-only scaling**. Guardrails fix both
  every time, and the UI shows it. Watch for new unsafe patterns in live runs.
- The **desktop IP 192.168.1.9 is baked in** (kubeconfig, certificate, agent URL). A DHCP change needs a
  cluster rebuild.
- After a **desktop reboot**, `scripts/live-up.sh` must run to restore the VXLAN checksum fix
  (see [`cluster.md`](cluster.md)).
- **No authentication.** Anyone who can reach :8600 can apply changes and spend LLM credit. It is currently
  bound to 0.0.0.0 on the home LAN.
- The Meta Model API profile's Glimmer and Wonder model IDs and prices are unverified. Testing uses Cerebras.
- With Cerebras, every tier costs the same, so the "all on the strategist" baseline equals the routed cost.

## Recent history

- PRs #1–#5: local k3d cluster + KEDA + apply, two-node SKU migration, live mode (vmagent, spike), GitOps
  overlays, the web UI.
- PR #6: provider profiles (Cerebras); guardrail against scale-to-zero for non-batch workloads.
- PR #7: UI redesign: isometric cluster, bullet charts, router flow, shadcn-style CSS, vendored fonts.
- PR #8: the real two-machine cluster (`join-node.sh`, VXLAN fixes), real hardware in the UI, the step strip,
  tweened usage, the CPU-trigger guardrail, a cost model matching the real cluster, skipping short-lived
  workloads, and this `docs/` folder.
