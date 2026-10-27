# Testing

## Automated (`pytest`, ~3 s, no cluster or network needed)

```bash
.venv/bin/python -m pytest -q
```

| file | covers |
|---|---|
| `tests/conftest.py` | session fixtures: the five demo scenarios reduced to Stage 1 stats, and a mock-mode analysis |
| `test_pipeline.py` | Stage 1 detects each anti-pattern, triage routing, recommendations and manifests, guardrails (never below observed usage; scale-to-zero only for batch workloads; peaky workloads keep a CPU trigger), the cron subset |
| `test_apply.py` | patches carry a header and run before ScaledObjects; migrations are skipped when no node has the target SKU |
| `test_demo_manifests.py` | demo manifests match the simulated scenarios and start on the current pool |
| `test_demo_app.py` | the demo app's `/work` endpoint burns CPU; the spike Job uses the demo app image |
| `test_gitops.py` | the overlay is committed on a branch and pushed; it builds with kustomize |
| `test_web.py` | page, report and config API, the `X-KubeSage` CSRF header, the font route, Kubernetes quantity parsing, `/api/cluster` (real node data, requests, usage, unscheduled pods), and that the page's script parses (`node --check`) |

Mock mode makes the pipeline deterministic, so tests never call an LLM.

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

Verified on 2026-10-27, live Cerebras profile:

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
   three pods were already running. The UI default is now 80 req/s, which crosses the target.

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
# make a copy of index.html: fonts from ./fonts, fetch() answered from the three JSON files, animations off
firefox --headless --screenshot out.png --window-size=1400,2900 file://$PWD/snapshot.html
```

Check light and dark (`data-theme="dark"` on `<html>`) and a 390 px phone width. The screenshots in
`docs/images/` were made this way from the live cluster.

UI behaviours to check by hand (not covered by the tests):

- **Selection:** clicking a pod, chip, bar row or flow line highlights that workload everywhere; Esc clears it.
- **Inspector:** shows the right panel for a machine, a workload, or the whole cluster.
- **Animation:**
  - after **Apply**, old pods fade out and new ones drop in on the laptop;
  - glowing cores ease to each new usage reading;
  - the violet lights follow machine CPU.
- **Step statuses:**
  - step 1 shows the last run;
  - step 2 shows applied/dry run with its counts;
  - step 3 counts down while load runs.
- **Keyboard:** Tab reaches pods, machines, bar rows, flow lines and chips; Enter and Space select.
- **Reduced motion:** with the OS setting on, nothing animates.
