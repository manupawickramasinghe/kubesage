# Testing

## Automated (`pytest`, no cluster or network needed)

```bash
.venv/bin/python -m pytest -q
```

| file | covers |
|---|---|
| `tests/conftest.py` | session fixtures: the five demo scenarios reduced to Stage 1 stats, and a mock-mode analysis |
| `test_pipeline.py` | Stage 1 signals, routing, recommendations, resource/replica guardrails, CPU scaling headroom, node sizing at CPU maximum replicas, invalid and overnight cron windows, overlapping schedule costs |
| `test_apply.py` | Patch ordering, Ready/schedulable target node capacity, migration skips, apply timeouts and stopping after failures, ignoring incomplete runs |
| `test_service.py` | Atomic report publication, failed writes preserve the previous report, existing runs cannot be overwritten |
| `test_demo_manifests.py` | demo manifests match the simulated scenarios and start on the current pool |
| `test_demo_app.py` | the demo app's `/work` endpoint burns CPU; the spike Job uses the demo app image |
| `test_gitops.py` | the overlay is committed on a branch and pushed; it builds with kustomize |
| `test_web.py` | Reports/history and selected runs, request validation, mutation locking, namespace isolation, fonts, quantity parsing, live plan/drift/readiness and traffic status, unavailable cluster data, and JavaScript parsing (`node --check`) |

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
failure states, measured traffic success/errors/latency, and responsive layouts in both themes at 390, 768 and 1400 px. Screenshots and `results.json`
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

Node sizing now includes maximum CPU autoscaler replicas. Some plans require additional capacity and can
cost more than the baseline; reporting a negative saving is valid.

## Manual: the UI

Screenshot without a browser driver: snapshot the page with the API stubbed, then use Firefox headless.

```bash
for e in config report cluster; do curl -s -H 'X-KubeSage: 1' http://127.0.0.1:8600/api/$e > $e.json; done
# make a copy of index.html: fonts from ./fonts, fetch() answered from the three JSON files, animations off
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
