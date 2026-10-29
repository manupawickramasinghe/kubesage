# Testing

Fresh verification on **29 October 2026** covered two pinned revisions:

| Revision | pytest | Chromium scenarios |
|---|---:|---:|
| Merged `main`, `2425375` (PR #15) | **124 passed** | **22/22 passed** |
| Pending PR #16–#19 stack, `641c74b` | **141 passed** | **37/37 passed** |
| PR #20 (the review's remaining findings), on top of that stack | **153 passed** | **40/40 passed** |

The full runs included local Kustomize checks using the pinned kubectl. API/browser fixtures were isolated;
these checks made no paid model calls or live cluster changes. On merged `main`, set `KUBESAGE_RING` to a
private temporary path and `KUBESAGE_RING_MB=4` before pytest so journal/agent fixtures do not write into the
running server's ring. PR #17 adds this isolation as a fixture. Missing kubectl skips one test on main and two
on the candidate, so a smaller passing count is not the full result above.

Additional targeted probes found remaining issues outside the green suites; see
[REVIEW_REPORT.md](REVIEW_REPORT.md) for finding statuses, pending fixes and the new live snapshot. Historical
live demonstrations below were not replayed for this report update.

## Automated (`pytest`, no cluster or network needed)

```bash
review_ring_dir=$(mktemp -d)
KUBESAGE_RING="$review_ring_dir/ring" KUBESAGE_RING_MB=4 .venv/bin/python -m pytest -q
```

| file | covers |
|---|---|
| `tests/conftest.py` | session fixtures: the five demo scenarios reduced to Stage 1 stats, a mock-mode analysis, and a private RAM ring so tests never write into a live server's `/dev/shm` ring |
| `test_pipeline.py` | Stage 1 signals, routing, recommendations, resource/replica guardrails, replica cuts against the measured total, a note for every bound the guardrails move, CPU scaling headroom, node sizing at CPU maximum replicas and by packing pods onto nodes, invalid and overnight cron windows, overlapping schedule costs; a plan the 20-replica cap can't make cover the measured total gets bigger pods, or is marked high risk with a follow-up |
| `test_apply.py` | Patch ordering, Ready/schedulable target node capacity, migration skips, removing the autoscaler a fixed replica count replaces, apply timeouts and stopping after failures, ignoring incomplete runs |
| `test_service.py` | Atomic report publication, failed writes preserve the previous report, existing runs cannot be overwritten |
| `test_demo_manifests.py` | demo manifests match the simulated scenarios and start on the current pool |
| `test_demo_app.py` | the demo app's `/work` endpoint burns CPU; the spike Job uses the demo app image and gets a unique name |
| `test_gitops.py` | the overlay is committed on a branch and pushed; it builds with kustomize; REPORT.md keeps the node-pool notes |
| `test_agents.py` | The RAM ring (wraparound, several writers, escape stripping, paging with a limit and a stream filter); loading `agents/` (new and disabled agents, unknown sizes); edit validation (exact match, paths, duplicate YAML keys, Python, Terraform SKUs); deterministic edits; the Thinker's guardrails (evidence, targets, namespaces, SKUs, positive quantities, request vs limit, today's node capacity); the change policy (kinds, namespace, resources) and binding edits to approved actions; every shipped lab app inside the policy; a full mock team run that commits to a throwaway git origin; Apply re-checking the commit and saving its outcome; alerts that resolve and fire again; stopping a case removes what a fix added; the security collector and its image allowlist; test-case manifests and status; the team, test-case and ring endpoints (validation, read-only ring, redacted dump, access log); the ring cursor never passes a segment started during a read; pod-level capacity on one real node, failing closed without node inventory; an approved action can't carry an image change, a privileged container or an extra object, and every rule-based fix type still passes; every lab object carries its test-case label, and stop / stop-all delete by it |
| `test_lab.py` | Inspection-shell command refusals/argument construction, safe trace Authorization placeholders, Lab status and journal behavior |
| `test_web.py` | Reports/history and selected runs, request validation, mutation locking, namespace isolation, fonts, quantity parsing, live plan/drift/readiness (a missing Deployment is never ready) and traffic status (a stranger's Job is ignored), unavailable cluster data, and JavaScript parsing (`node --check`) |
| `test_llm.py` | The live client with canned provider replies: an empty reply retried with less reasoning, two empty replies or a refusal named in the error, invalid JSON fed back once; malformed provider envelopes (`choices: [null]`, a non-mapping message, non-text content) fail as a classified `LLMError` with the exchanges kept, and content parts are read as text |

Mock mode makes the pipeline deterministic, so tests never call an LLM.

## Optional browser regression checks

With Playwright and Chromium installed, run the separate smoke script. It starts a local page with mocked
APIs, so it spends no LLM credit and makes no cluster changes:

```bash
.venv/bin/python tests/browser_smoke.py --output-dir /tmp/kubesage-browser
# Or use an existing Chromium installation:
.venv/bin/python tests/browser_smoke.py --executable /path/to/chromium --output-dir /tmp/kubesage-browser
```

It checks selection, validation/apply/reload, traffic/reset, history-pinned actions, downloads, filters,
failure states, measured traffic success/errors/latency, the agent team panel (untrusted model text stays text),
the tmux mirror (no input elements, never POSTs, panes visible at phone width, quiet panes keep their lines on first
load), the tmux panel beside the other views (beside, not over, the view; own window and zoom; remembered after a
reload; a sheet on a tablet; the header keeps its rows; with the chat, the view keeps at least 560 px), the agent chat (every hand-off in order, guardrail refusals,
reasoning from this run's window only, kept polled on FinOps, an earlier run, a live run's typing line, closed state
remembered, a drawer on a tablet, opened from the run bar on a phone), the demo guide, the plan on screen
being the plan Apply sends, errors reaching whichever view is open, keyboard focus and shell Tab, the Wire
reloading after operations, unknown usage shown as unknown (a finished Job pod doesn't count as unsampled), the Demo saying what it measured
(failed or unknown traffic results, team-run time to commit, an unknown plan state, a pool that costs more), and responsive layouts in both themes at 390, 768 and
1400 px. Toolbar Start/Stop testcase and Run agents controls do POST;
read-only describes the terminal mirror, not every control in the view. Screenshots and `results.json`
are written to the output directory. Normal pytest runs do not require a browser. Check Firefox/Zen manually
as well when available.

## Manual: the real two-machine cluster

Run these after any change to scripts, networking or the cluster API:

```bash
export KUBECONFIG=$PWD/.bin/kubeconfig PATH=$PWD/.bin:$PATH
kubectl get nodes -L kubesage.io/sku -o wide          # both Ready; D8s_v5 desktop, F8s_v2 laptop
kubectl top nodes                                      # both report usage
# cross-machine pod traffic (DNS + HTTP from a laptop pod to desktop pods):
kubectl -n demo run nettest --rm -i --restart=Never --image=python:3.13.12-alpine3.22 \
  --overrides='{"spec":{"nodeName":"manupa-hnx"}}' --command -- python -c '
import socket, urllib.request, urllib.error
print(socket.gethostbyname("checkout-web.demo.svc.cluster.local"))
try: urllib.request.urlopen("http://checkout-web.demo.svc.cluster.local:8080/", timeout=5)
except urllib.error.HTTPError as e: print("reached, HTTP", e.code)'
```

## Manual: the demo flow end to end

Historical live Cerebras example from 2026-10-27 (before the capacity-sizing corrections):

1. **Reset demo**: 8 pods on `pop-os` (desktop), none on `manupa-hnx`.
2. **Analyze**: 6 calls, ~$0.011.
   - payment-service 2 CPU / 4 GiB → 220m / 456Mi;
   - report-worker scales to zero outside 01:50–03:10;
   - auth-api diagnosed as a memory leak;
   - node pool 1× D8s_v5 → 1× F8s_v2.
   - Both guardrails fired on the model's checkout-web plan: min 0 → 1, and a CPU trigger was added.
3. **Apply fixes**: 11/11 steps ok. Every pod rescheduled onto `manupa-hnx` and Running there; the desktop
   slab went empty.
4. **Send traffic** (40 req/s × 50 ms for 150 s): checkout-web CPU rose from 10% to ~60% of its 70% target, and
   the laptop's whole-machine CPU from 1.7 to ~3.4 cores. No pods were added: the plan's max was 3 and
   three pods were already running.
5. **Send traffic at 60 req/s** (the UI default since the code review of PR #10; 80 req/s hit a 4-replica
   ceiling with HTTP errors and 7–11 s p95). Live run on 2026-10-27, checkout-web min 2 / max 6, CPU target 70%:
   - 0–80 s: the load arrives all at once on 2–3 pods. CPU reached 130% of target, the autoscaler went
     2 → 4 → 6, and the sampled intervals showed 23 failed requests with p95 up to 15 s.
   - 90–180 s: 6 pods, CPU 73–74%, **0 errors**, p50 ~75 ms, p95 170–240 ms, ~600 requests per 10 s.
   So 60 req/s is sustainable once checkout-web has scaled; expect a slow first minute, because the
   load starts at full rate.
6. **Same at 60 req/s on the laptop** (2026-10-28, after Apply moved every pod to `manupa-hnx`; plan min 1 / max 4):
   - checkout-web went 3 → 4 pods within ~30 s, and CPU settled at 105–109% of the 70% target. That is
     the replica ceiling, but it kept serving.
   - The laptop went from 1.2 to 4.2–4.6 cores of its 8 threads.
   - First minute: 4 failed requests, p95 up to 9 s. After that: 0 errors, p50 ~140 ms, p95 330–410 ms.
   - For comparison, the earlier 80 req/s run on a 4-pod ceiling had 12–35 errors per 10 s and a 7–11 s p95.

For a new run, verify the current plan rather than expecting the historical SKU or savings above:

1. Reset and Analyze once. Confirm the run's Live/Mock mode, review its guardrails and required node count.
2. Enable **Validate only**. Check the validated and skipped counts; live plan status must not change merely
   because validation succeeded.
3. Apply the selected run. Inspect Activity for every written, skipped or failed step. Migration must be
   skipped if there are too few Ready, schedulable machines with the target SKU; safe resource changes can
   still succeed. Confirm the latest plan's live specs and rollout readiness separately, including after a
   browser reload.
4. Send traffic once. Watch CPU against its target and current/max replicas. Extra pods require load above
   target and replica headroom. Check traffic job logs when assessing request success; a countdown alone is
   not evidence of successful traffic.
5. Reset at the end. Saved analysis reports should remain available, while live plan status reflects the
   reset cluster.

Node sizing uses scheduled peaks. Maximum CPU autoscaler demand is checked separately as `demand_burst`;
notes report modelled fit/shortfall. Some plans require additional capacity and can cost more than the
baseline; reporting a negative saving is valid.

## Manual: cleanup

`scripts/cleanup.sh --check` and `--dry-run` are safe to run at any time. For a real teardown without losing the
demo, point it at a throwaway cluster:

```bash
.bin/k3d cluster create kubesage-cleanuptest --image rancher/k3s:v1.36.4-k3s1 --no-lb \
  --kubeconfig-update-default=false --kubeconfig-switch-context=false --wait
CLUSTER_NAME=kubesage-cleanuptest scripts/cleanup.sh --yes --skip-nodes   # also stops kubesage serve
```

This was done on 2026-10-27. The test cluster, its network and volume, and the attachment of an extra container
were all removed; the real cluster and `.bin/kubeconfig` were untouched. The laptop path is tested with the
leave-and-rejoin cycle in [cleanup.md](cleanup.md#how-this-was-tested), which has not been run yet.

## Manual: the UI

Screenshot without a browser driver: snapshot the page with the API stubbed, then use Firefox headless.

```bash
for e in config report cluster; do curl -s -H 'X-KubeSage: 1' http://127.0.0.1:8600/api/$e > $e.json; done
# make a copy of dashboard/: fonts from ./fonts, fetch() answered from the three JSON files, animations off
firefox --headless --screenshot out.png --window-size=1400,2900 file://$PWD/snapshot.html
```

Check light and dark (`data-theme="dark"` on `<html>`) and a 390 px phone width. The screenshots in
`docs/images/` were made this way from the live cluster.

Additional checks against the live server:

- **Selection:** clicking a pod, chip, bar row or flow line highlights that workload everywhere; Esc clears it.
- **Inspector:** shows the right panel for a machine, a workload, or the whole cluster.
- **Animation:**
  - after **Apply**, old pods fade out and new ones drop in on the laptop;
  - glowing cores ease to each new usage reading;
  - the violet lights follow machine CPU.
- **Step statuses:**
  - step 1 shows the selected run;
  - step 2 separates live plan matches, rollout readiness and session validation counts;
  - step 3 shows CPU target and replica limits, and rediscovers active traffic after reload.
- **History/downloads:** choose an older run; Apply and GitOps must send that run ID, and downloads must contain
  its report/manifests. Live cluster readings keep updating. Latest-run drift verification must not be
  presented as verification of the historical selection.
- **Evidence:** charts retain reservations at analysis time after Apply; the inspector shows live requests.
  Workload search/status filters and field-level plan differences remain usable while polling.
- **Failures:** an unavailable metrics backend produces a readable Analyze error; unavailable Kubernetes
  shows **Cluster not reachable**. Test API failures through mocks instead of taking down the cluster.
- **Keyboard:** Tab reaches pods, machines, bar rows, flow lines and chips; Enter and Space select.
- **Reduced motion:** with the OS setting on, nothing animates.
