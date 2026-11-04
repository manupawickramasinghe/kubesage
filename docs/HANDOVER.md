# Handover: KubeSage

You are taking over **KubeSage**, an AI SRE team for Kubernetes built for a hackathon. **Demo day is
4 November 2026.** This file is where to start: the rules, what exists, the state it was left in, how to run and
present it, and what is still open.

Snapshot taken **1 November 2026** (Asia/Colombo). Repo: `~/kubesage` on the 8600G desktop, GitHub
`manupawickramasinghe/kubesage` (**public**), default branch `main`. Every pull request so far is merged except #12, a
PR the agent team opened during testing (closed). This handover is #22.

## Rules you must follow

1. **Git identity.** Commit only as `M.N Wickramasinghe <manupawickramasinghe@users.noreply.github.com>`, the
   user's account. **No `Co-Authored-By` trailers. No "Generated with …" lines in commits or PR bodies. Never add an
   AI or bot as a collaborator.**
2. **Secrets.** API keys live only in `~/kubesage/.env` (gitignored, mode 600). Never print, log, commit or paste a
   key. The repo is public: `git grep` for key patterns before pushing. The laptop's sudo password is not stored
   anywhere; ask the user if you need it.
3. **Workflow.** Branch from `main`, open a PR, say what you verified. Don't push to `main`. Merge only when the user
   asks.
4. **Shared machines.** Both machines run other important services:
   - the **desktop** runs Frigate, a `rikugan-alertmanager` container and more: touch only `k3d-kubesage-*` and
     `kubesage-*` containers;
   - the **laptop** runs Pi-hole (the LAN's DNS) and GitHub runners: don't touch them.
   - Ask before anything destructive: `cleanup.sh`, `cluster-down.sh`, `leave-node.sh`, uninstalls, reboots.
     `scripts/cleanup.sh --check` and `--dry-run` change nothing.
5. **Off-limits** (the user's standing rules): repositories of the `Rikugan` / `Rikugan-LK` organisations (and the
   `rikugan-alertmanager` container); `esp32-blue-audio-puck`; branch `test/upstream-0.18-rust-integration` in
   `Zektopic/frigate`.
6. **The web never controls a terminal.** The Control Center has no login and listens on the LAN, so the lab shell is
   read-only and the tmux views are mirrors of the RAM ring, with no write endpoint. Keep it that way. Don't start the
   server with `--agent-prs` on the LAN unless the user asks: it lets anyone on the network open PRs on the public repo.
7. **Costs.** A live **Analyze** spends about $0.01 of the user's Cerebras credit and an agent-team run about $0.02.
   **Send traffic** burns about 4 real cores on the laptop while it runs. Fine for testing; don't loop them.

## What KubeSage is, and what's new about it

It finds capacity a cluster pays for and doesn't use, right-sizes it, and fixes incidents, with a person approving
every change. The five points the demo video leads with, each backed by the code:

| | what it means | where |
|---|---|---|
| **FinOps + SRE, one system** | The tool that plans the savings also fixes what breaks. Today they are two workflows (Analyze/Apply and the agent team), not one loop | `kubesage/pipeline.py`, `kubesage/agents/` |
| **Built for the rush** | From 30 days of usage it finds recurring peaks and writes a KEDA rule: cron windows that add pods ~10 minutes early, plus a CPU trigger for surges no schedule predicts. The node pool is sized for the scheduled peaks | `kubesage/metrics/stats.py`, `kubesage/recommend/` |
| **Cents, not dollars** | Statistics need no model. One cheap triage call; more reasoning only for hard cases. Analysis ≈ $0.011, a team run ≈ $0.02 | `kubesage/llm/`, `config/models*.yaml` |
| **Evidence or it's dropped** | A finding that cites no signal an agent collected is dropped; guardrails reject unsafe or impossible actions | `kubesage/agents/team.py`, `kubesage/recommend/guardrails.py` |
| **A person decides** | FinOps models return parameters that KubeSage renders; the Executor's edits are checked by policy and committed on their own branch. Nothing reaches the cluster until someone presses **Apply** | `kubesage/agents/policy.py`, `kubesage/apply.py` |

Read next, about 30 minutes: the [README](../README.md), then [ui-guide.md](ui-guide.md) (what every part of the UI
means), [agents.md](agents.md), [cluster.md](cluster.md), [operations.md](operations.md) (the runbook) and
[testing.md](testing.md).

## What exists

```
browser ──▶ kubesage serve (desktop :8600) ──▶ VictoriaMetrics (127.0.0.1:8428) ◀── vmagent (in cluster)
               │        │                  ──▶ vmalert ──▶ Alertmanager (127.0.0.1:9193) ──▶ agent team
               │        └──▶ Cerebras: gpt-oss-120b (FinOps tiers, Thinker, Executor), Qwen 3.8 27B (sub-agents)
               └──▶ Kubernetes API 192.168.1.9:6443
                      ├─ k3d-kubesage-server-0 = 8600G desktop "pop-os", control plane, stands in for an Azure D8s_v5
                      └─ manupa-hnx            = 3500U laptop, worker,         stands in for an Azure F8s_v2
Grafana 127.0.0.1:3300 · RAM ring (500 MiB) ──▶ tmux view and the dashboard's tmux panel
```

| path | what |
|---|---|
| `kubesage/` | the Python package: `pipeline.py` (analysis), `metrics/`, `llm/`, `recommend/` (guardrails, costing, rendering), `apply.py`, `agents/` (team, policy, edits), `lab/` (test cases), `ring.py`, `web/` (server and APIs), `cli.py` |
| `dashboard/` | the web UI: plain scripts loaded in order, no build step, no CDN; see [dashboard/README.md](../dashboard/README.md) |
| `agents/` | each agent's prompt and settings, and `rules/` (the team's plain-English rules) |
| `config/` | model profiles (`models.cerebras.yaml` is the one in use) and the SKU catalog |
| `gitops/` | the demo apps and the test cases (`apps/`), and `infra/nodepools.tf` (the Terraform the Executor may propose changes to) |
| `deploy/` | vmagent, alert rules and the compose stack |
| `scripts/` | cluster up/down, join/leave the laptop, `live-up.sh`, `demo-reset.sh`, `cleanup.sh` |
| `demo/` | the demo video, its slides and pictures, every screenshot, and the tools that rebuild the video |
| `tests/` | pytest (153) and `browser_smoke.py` (40 UI checks against a mocked API) |

## State on 1 November

- **Cluster:** both nodes Ready. The `demo` namespace is in its **"before"** state: all eight pods on the desktop with
  their oversized requests. No test case is running.
- **Containers on the desktop:** `k3d-kubesage-server-0`, `k3d-kubesage-serverlb`, and `kubesage-victoriametrics-1`,
  `-grafana-1`, `-vmalert-1`, `-alertmanager-1`.
- **The LAN Control Center** runs on `0.0.0.0:8600` (pid 691448) from the worktree `~/kubesage-agents` with the
  Cerebras profile, started from a temporary script that won't survive a reboot. To restart it the standard way, see
  [Run it](#run-it).
- **Checkouts:** `~/kubesage` is still on Codex's branch `docs/review-report-2026-10-29` (PR #14, merged). Once Codex
  is done with it, `git switch main && git pull`. The worktree `~/kubesage-agents` is where the recent work was done;
  `git worktree list` shows both. `.bin` in the worktree is an untracked symlink: never `git add` it.
- **Latest runs:** analysis `20261029-114842-131212` and agent-team run `20261029-034845`, both in
  `~/kubesage-agents/out/`. These are the runs the demo video replays.
- **Tests:** `pytest -q` 153 passed; `tests/browser_smoke.py` 40/40 (both on 1 November, `main` at `a2465bd`).

## Run it

```bash
cd ~/kubesage
export KUBECONFIG=$PWD/.bin/kubeconfig PATH=$PWD/.bin:$PATH
kubectl get nodes -L kubesage.io/sku -o wide          # both Ready?
docker compose up -d                                   # VictoriaMetrics, Grafana, vmalert, Alertmanager
set -a; . ./.env; set +a
KUBESAGE_MODELS=cerebras .venv/bin/kubesage serve --host 0.0.0.0   # http://192.168.1.9:8600
.venv/bin/python -m pytest -q                          # 153 tests, ~20 s
```

Only one server can hold :8600: check `ss -ltnp | grep 8600` and stop the old one first. Restart the server after a
Python change; files in `dashboard/` are read on every request, so a page edit needs only a reload. After a desktop
reboot, run `scripts/live-up.sh` first (it restores the VXLAN checksum fix; see [operations.md](operations.md)).

## Demo day

Follow the checklist in [operations.md](operations.md#demo-day-checklist); the Demo tab's **Before you present** box
checks most of it live. In short: both nodes Ready, **Reset demo**, stop any test case, the header says **Live LLM**,
rehearse once, Reset again.

- **On venue Wi-Fi**, don't expose :8600. Bind to localhost and tunnel from the presenting laptop:
  `ssh -L 8600:localhost:8600 -L 3300:localhost:3300 manupa@192.168.1.9`.
- **Timings:** Analyze ~5 s; pods settle ~1 min after Apply; the traffic runs 3 min; a test case's alert takes about a
  minute, then the team waits 45 s and runs for 15–35 s.
- **No internet:** start the server with `--mode mock` (the header shows "Mock LLM").
- **Backup:** if the live demo fails, play the video, [`demo/video/kubesage-demo.mp4`](../demo/video/kubesage-demo.mp4).

## The demo video

[`demo/video/kubesage-demo.mp4`](../demo/video/kubesage-demo.mp4): 2 min 53 s, 1600×900, narrated, with subtitles
(remade on 4 November for the Meta Muse models and the business calendar).

- **Structure:** slides first (what's new, a hypothetical customer cluster called *shop-prod*, "Built for the rush" with
  the business calendar, and how it works), then 51 s of the real dashboard: Analyze with the Forecast panel, Apply, a
  bad release, the agent team and the fix. The user asked for this: no home-lab setup on screen, the summary before the
  website, the website in a minute, the whole video under three.
- **What is real:** the website part replays KubeSage's recorded runs, relabelled onto the hypothetical cluster's two
  Azure VMs: the live Meta Muse analysis of 1 November (−70% reservation cost, node pool +32%, $0.27) and the agent-team
  run of 29 October (Cerebras profile). The header says "Simulation · hypothetical cluster" throughout.
  [demo/README.md](../demo/README.md) says exactly what is recorded, hypothetical, shortened or illustrative.
- **How it was made:**
  - the voice is Kokoro (`af_heart`), which runs locally;
  - the pictures were generated by agy (Gemini);
  - agy edited the narration for clarity and fact-checked it read-only; Codex, which checked earlier versions, was
    logged out ([demo/tools/narration-review.md](../demo/tools/narration-review.md)).
- **Rebuild:** `demo/tools/make_video.sh`, about 4 minutes. Keep the whole video under 3:00; it has about 7 s of room.

## Open items

**Do before the demo:**

1. **Rotate the Cerebras API key** at the provider, then update `~/kubesage/.env` and restart the server. It has been
   handled in chat sessions.
2. **Change the laptop's password**, for the same reason.
3. **Reserve 192.168.1.9 for the desktop** in DHCP. The IP is baked into the kubeconfig, the API certificate and the
   laptop's agent URL; a change means rebuilding the cluster.
4. **Add a LICENSE.** The repo is public and has none, so by default nobody may reuse it. Ask the user which license.

**Gaps behind the pitch** (the video words around them; build them before claiming more):

- **No sales calendar.** KubeSage learns weekly peaks only: its cron subset is `M H * * DOW`
  (`kubesage/recommend/cron.py`). Black Friday or Christmas windows have to be added to the KEDA rule by engineers,
  and the node-pool sizing doesn't see them. A "planned events" input (dates → cron windows → pool sizing) would make
  that a feature.
- **Not one loop yet.** The FinOps analysis and the agent team don't hand work to each other. Nothing triggers a team
  run after an Apply, and agent findings don't feed the next analysis.
- **Synthetic history.** The 30 days of usage are a synthetic backfill (`kubesage simulate`) plus a live tail. Machines,
  pods, live usage, scaling and LLM costs are real; prices are Azure list prices.

**Known limits:**

- No authentication on the Control Center (see rule 6).
- With the Cerebras profile every tier is `gpt-oss-120b` at a different reasoning effort, so the "all on the
  strategist" baseline equals the routed cost.
- The Meta Model API profile (default; judges use Muse) was fixed Nov 2026: Glimmer is not an API model,
  so triage/small run on Muse Spark 1.3 and the strategist/large on Muse Wonder 1.0, prices verified.
- `scripts/cleanup.sh`'s laptop path (`leave-node.sh`) has never been run for real.
  [cleanup.md](cleanup.md) has a leave-and-rejoin test.
- The project review's findings are all fixed (PR #20). [REVIEW_REPORT.md](REVIEW_REPORT.md) section 12 has the
  status.

## History

| PRs | what |
|---|---|
| #1–#5 | local k3d cluster and KEDA, the two-node SKU migration, live mode (vmagent, traffic spike), GitOps overlays, the web UI |
| #6–#8 | provider profiles (Cerebras), the UI redesign (isometric cluster, charts, router flow), the real two-machine cluster and the docs |
| #9–#11 | the cleanup script, UX and scaling-safety fixes, the "Under the hood" lab view |
| #12 | a PR the agent team opened itself while being tested (closed, not merged) |
| #13 | the agent team: sub-agents, Thinker, Executor, live test cases, alerts, the RAM ring and tmux view |
| #14–#16 | the project review report; the Demo tab for judges and the `dashboard/` folder; the first round of review fixes |
| #17–#19 | the tmux panel beside every view, the agent chat sidebar, the README rewrite |
| #20 | the review's remaining findings: the Executor's semantic diff and action policy, pod-level capacity, the ring race, test-case ownership labels, cleanup, honest Demo figures |
| #21 | the `demo/` folder: every screenshot, the narrated demo video and the tools that rebuild it |
