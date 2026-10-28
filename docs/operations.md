# Runbook

All commands run on the **8600G desktop** in `~/kubesage` unless they say otherwise.

```bash
export KUBECONFIG=$PWD/.bin/kubeconfig PATH=$PWD/.bin:$PATH   # to use kubectl yourself
set -a; . ./.env; set +a                                      # CEREBRAS_API_KEY for live LLM calls
```

## Start the Control Center

```bash
KUBESAGE_MODELS=cerebras .venv/bin/kubesage serve --host 0.0.0.0   # http://192.168.1.9:8600
```

Without `--host 0.0.0.0` it only answers on localhost. There's no login: anyone who can reach port 8600 can
apply changes and spend LLM credits. Use it on a home network or Tailscale, and keep it on localhost (with an
SSH tunnel) on venue Wi-Fi:

```bash
ssh -L 8600:localhost:8600 -L 3300:localhost:3300 manupa@192.168.1.9   # from the presenting laptop
```

Grafana is at http://localhost:3300 on the desktop: anonymous view, admin/admin to edit.

For the agent team: `docker compose up -d` also starts vmalert and Alertmanager (127.0.0.1:9193). Add
`--agent-prs` to let the Executor open pull requests. `kubesage tmux` opens the tmux session over the RAM ring
(`kubesage tmux --no-attach` to only create it). See [agents.md](agents.md).

## After a reboot

| machine | comes back on its own | then run |
|---|---|---|
| desktop | Docker restarts VictoriaMetrics, Grafana and both k3d containers | `scripts/live-up.sh` (re-applies the VXLAN checksum fix), then `kubesage serve` |
| laptop | k3s agent (systemd) and the udev fix | nothing |

Check: `kubectl get nodes` shows both Ready, and `kubectl top nodes` lists both.

## Demo day checklist

1. Both machines on the same network. Ideally the desktop keeps 192.168.1.9 (a DHCP reservation).
2. `kubectl get nodes`: both Ready. `kubectl top nodes`: both report usage.
3. **Reset demo** in the UI: every pod back on the desktop, with big requests.
4. Header shows **Live LLM**. If it shows Mock, the API key isn't in the server's environment.
5. Rehearse 1 → 2 → 3 once, then Reset. A full run takes ~5 s for Analyze, ~1 min for pods to settle after
   Apply, and 3 min for the traffic.
6. Fallback with no internet: start the server with `--mode mock`. Analyze then uses deterministic answers and
   the UI shows "Mock LLM".

## Everyday commands

| task | command |
|---|---|
| Analyze from the CLI | `KUBESAGE_MODELS=cerebras .venv/bin/kubesage analyze` (writes `out/<run>/`) |
| Preview an apply | `.venv/bin/kubesage apply --dry-run` |
| Apply the latest run | `.venv/bin/kubesage apply` |
| Back to the "before" state | `scripts/demo-reset.sh` |
| Send traffic | `.venv/bin/kubesage spike --rps 40 --work-ms 50 --seconds 180` |
| Watch scaling | `kubectl -n demo get hpa,pods -o wide -w` |
| Regenerate history | `.venv/bin/kubesage simulate`, then `scripts/live-up.sh` |
| What is still installed? | `scripts/cleanup.sh --check` |
| Remove the clusters (both machines), keep metrics history | `scripts/cleanup.sh`, or `scripts/cluster-down.sh` to skip the prompt; see [cleanup.md](cleanup.md) |
| Remove everything | `scripts/cleanup.sh --all` |
| Stop metrics but keep the data | `docker compose down` (the volume keeps it) |

## Where things are

| thing | location |
|---|---|
| Analysis runs | `out/<YYYYmmdd-HHMMSS>/report.json` and `manifests/` |
| kubeconfig | `.bin/kubeconfig` (server `https://192.168.1.9:6443`) |
| Pinned tools | `.bin/k3d`, `.bin/kubectl` (from `scripts/install-tools.sh`) |
| API keys | `.env` (gitignored, mode 600) |
| Server log | wherever you redirect `kubesage serve`'s output |
| Laptop agent logs | `journalctl -u k3s-agent` on the laptop |
| Server node logs | `docker logs k3d-kubesage-server-0` |

## When something breaks

| symptom | fix |
|---|---|
| UI: "Cluster not reachable" | `kubectl get nodes`. If it fails, `docker ps \| grep k3d` and `docker start k3d-kubesage-server-0 k3d-kubesage-serverlb`. |
| Analyze: "metrics query failed" | `docker compose up -d`, and check http://localhost:8428 |
| Analyze: "no workloads found" | `kubesage simulate`, then `scripts/live-up.sh` |
| Analyze: "LLM call failed" | Check the key in `.env` and network access. Or use `--mode mock`. |
| Apply: a step fails | The Activity log shows `kubectl`'s message. A `--dry-run` first shows it without changing anything. |
| Pods stay on the desktop after Apply | The run chose a SKU no machine has as its label. `kubectl get nodes -L kubesage.io/sku` and `kubectl label --overwrite node/manupa-hnx kubesage.io/sku=<sku>`, then Apply again. |
| Cross-machine traffic times out | See the VXLAN checksum trap in [cluster.md](cluster.md#how-the-networking-works-and-the-two-traps). |
| Send traffic adds no pods | checkout-web is already at its max replicas, or has no CPU trigger (check its row in Recommendations; the guardrail adds one for peaky workloads). |
