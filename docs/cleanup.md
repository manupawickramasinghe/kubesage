# Cleaning up

One script undoes everything KubeSage set up, on the 8600G desktop and on every machine joined to the
cluster: `scripts/cleanup.sh`. It always shows what it will remove and asks before doing anything.

## Which command

| goal | run (on the desktop, in `~/kubesage`) |
|---|---|
| See what is still installed or running | `scripts/cleanup.sh --check` |
| Preview a cleanup | `scripts/cleanup.sh --dry-run` (add `--all` to preview the full wipe) |
| **Remove the clusters** and keep the metrics history for a quick rebuild | `scripts/cleanup.sh` |
| **Remove everything**: clusters, metrics and their history, images, tools, runs | `scripts/cleanup.sh --all` |
| Remove only the laptop from the cluster | `scripts/leave-node.sh manupa@192.168.1.92` |
| Put the demo back to its "before" state (not a cleanup) | `scripts/demo-reset.sh`, or **Reset demo** in the UI |

`scripts/cluster-down.sh` is shorthand for `scripts/cleanup.sh --yes`: no confirmation prompt. It accepts the
same options.

The laptop part needs its **sudo password**. The script asks once for all joined machines; or set
`SUDO_PASSWORD=…` for that one command. It goes to the laptop over SSH stdin and is never written anywhere.
SSH itself must work with a key, as it does today.

## What each level removes

```mermaid
flowchart LR
  subgraph default["scripts/cleanup.sh"]
    ui["kubesage serve<br/>(stopped)"]
    agent["laptop: k3s agent, its network<br/>interfaces, udev rule, ufw rules"]
    k3d["desktop: k3d cluster<br/>containers, network, image volume"]
    kc[".bin/kubeconfig"]
  end
  subgraph all["--all adds"]
    vm["VictoriaMetrics + Grafana<br/>and ALL metrics history"]
    img["docker images:<br/>k3s, k3d, VictoriaMetrics, Grafana"]
    dirs[".bin/ (k3d, kubectl)<br/>out/ (analysis runs)"]
  end
```

| | `cleanup.sh` | `cleanup.sh --all` |
|---|---|---|
| `kubesage serve` processes | stopped | stopped |
| **Laptop:** node drained and deleted; k3s agent uninstalled (binary, systemd unit, `/var/lib/rancher`, `flannel.1`/`cni0`, k3s' iptables chains); `/etc/udev/rules.d/90-kubesage-flannel.rules`; ufw rules commented "kubesage" | removed | removed |
| **Desktop:** k3d containers `k3d-kubesage-server-0` and `k3d-kubesage-serverlb`, network `k3d-kubesage`, volume `k3d-kubesage-images` (everything inside the cluster goes with them: KEDA, monitoring, demo pods) | removed | removed |
| `.bin/kubeconfig`, only if it belongs to this cluster | removed | removed |
| VictoriaMetrics + Grafana containers, network `kubesage_default` | kept | removed |
| Volume `kubesage_vmdata`: **all metrics history**, the 30-day backfill plus everything recorded live | kept | **removed** |
| Images `rancher/k3s`, `ghcr.io/k3d-io/k3d-*`, `victoriametrics/victoria-metrics`, `grafana/grafana:11.6.0` | kept | removed, unless another container still uses them, or another k3d cluster exists |
| `.bin/` (k3d, kubectl, `joined-nodes`) and `out/` (analysis runs) | kept | removed |

**Intended preservation:** `.env` (API keys), `.venv`, the repo and its git history, the GitHub repo, your
Obsidian notes and `~/.kube/config`. The script does not stop or delete unrelated Docker containers, but its
current network-detach loop can disconnect any container attached to the k3d network without checking
KubeSage ownership. Remote uninstall also has incomplete failure handling. These are open findings F04/F20
in the [29 October review](REVIEW_REPORT.md), so this is not a guarantee that unrelated services remain
unaffected. PR #16 addresses the network-ownership loop and direct uninstall error handling, but
`cleanup.sh --all` can still delete the recovery registry after a failed remote leave. See the updated
report for the pending-fix assessment. No new teardown or laptop uninstall was performed for that review.

## Finding the joined machines

`join-node.sh` writes each machine it joins to `.bin/joined-nodes` (today: `manupa@192.168.1.92`). Cleanup also
asks the live cluster for any non-k3d node and adds it as `$USER@<its IP>`. Set `SSH_USER` if the login there is
different. Other options:

- `--node user@host` to add a machine by hand;
- `--skip-nodes` to leave all joined machines alone. They then keep a k3s agent that retries a server that's
  gone; run `leave-node.sh` for each later.

## Order of operations

The script runs these in order. Use the same order by hand if you ever need to.

1. Stop `kubesage serve`, so nothing keeps polling the cluster.
2. For each joined machine (needs the cluster still up, to drain its node):

   ```bash
   kubectl drain node/manupa-hnx --ignore-daemonsets --delete-emptydir-data
   kubectl delete node/manupa-hnx
   ssh manupa@192.168.1.92 'sudo /usr/local/bin/k3s-agent-uninstall.sh; sudo rm -f /etc/udev/rules.d/90-kubesage-flannel.rules'
   ssh manupa@192.168.1.92 'sudo ufw status numbered | grep kubesage'   # delete those, if any: sudo ufw delete <n>
   ```

3. Detach VictoriaMetrics from the cluster's network (`live-up.sh` attached it), then delete the cluster. The
   script detaches only containers of KubeSage's compose project (label
   `com.docker.compose.project=kubesage`). Any other container on `k3d-kubesage` is left attached and named in
   a warning, and the network then stays until you detach it yourself and run cleanup again:

   ```bash
   docker network disconnect -f k3d-kubesage kubesage-victoriametrics-1
   .bin/k3d cluster delete kubesage
   rm -f .bin/kubeconfig
   ```

   Without the k3d binary, remove the containers labelled `k3d.cluster=kubesage`, the network `k3d-kubesage`
   and the volume `k3d-kubesage-images` with plain `docker rm -f`, `docker network rm` and `docker volume rm`.

4. `--all` only:

   ```bash
   docker compose down --volumes --remove-orphans
   docker rmi rancher/k3s:v1.36.4-k3s1 ghcr.io/k3d-io/k3d-proxy:5.9.0 ghcr.io/k3d-io/k3d-tools:5.9.0 \
     victoriametrics/victoria-metrics:v1.110.0 grafana/grafana:11.6.0
   rm -rf .bin out
   ```

5. Run the same report as `--check`.

## Checking it worked

`scripts/cleanup.sh --check` lists every item per machine:
- ✓ gone;
- ✗ still there, with the evidence;
- · kept on purpose (without `--all`).

Its exit status is 0 only when nothing is left, so it also works in scripts. A full cleanup ends with this
report automatically.

```text
pop-os (this machine):
  ✓ kubesage serve stopped
  ✓ k3d cluster 'kubesage' deleted
  ✓ its docker network and volume gone
  ✓ kubeconfig of 'kubesage' removed
  ✓ ports 6443 8472 10250 8600 free
  · kept: metrics stack                  kubesage-grafana-1 kubesage-victoriametrics-1
  · kept: metrics history                kubesage_vmdata
    (--all removes these too)
manupa@192.168.1.92:
  ✓ k3s agent uninstalled
  ✓ flannel/cni interfaces gone
  ✓ VXLAN udev rule removed

Nothing left behind.
```

Two things it can't verify: the laptop's ufw rules (reading them needs root; the agent's rules were never
added, because ufw is inactive there), and `iptables` chains. For a manual look, run
`sudo iptables-save | grep -ciE 'kube|flannel|cni'` on the laptop; it should print 0.

## After a cleanup

| next | do |
|---|---|
| Rebuild the demo | Follow "Set it up from scratch" in [cluster.md](cluster.md). After a plain cleanup the history is still there, so skip `kubesage simulate`. |
| Done with the project for good | `scripts/cleanup.sh --all`; delete `.env` and rotate the API key at the provider; optionally `rm -rf .venv` |
| Free the LAN ports | Already free: 6443, 8472 and 10250 were Docker publishes of the k3d server, and 8600 was `kubesage serve` |

## How this was tested

- `--check` and `--dry-run` were run against the live two-machine setup. They found every component on both
  machines: the server process, both k3d containers, the network, the volume, the kubeconfig, the ports, and
  on the laptop the binary, unit, data directory, interfaces and udev rule.
- **A real teardown** ran against a throwaway cluster, `CLUSTER_NAME=kubesage-cleanuptest`, with an extra
  container attached to its network like VictoriaMetrics.
  - Containers, network and volume were removed, and the extra container was detached. (That was before the
    ownership check: today only a container of KubeSage's compose project is detached, and any other one is
    left attached and named, so the network stays.)
  - The real cluster and its kubeconfig were byte-for-byte untouched; k3d is given a scratch kubeconfig for that.
- **Not run yet: the laptop part** (`leave-node.sh`, uninstalling k3s there). It stops at the first failure on
  the machine, prints the uninstaller's last lines, checks that the binary, unit, data directory and udev rule
  are gone, and only then removes the machine from `.bin/joined-nodes`. The remote part was exercised against
  a scratch directory (success, a failing uninstaller, files left behind), not on the laptop. To test it without
  losing the laptop, run a leave-and-rejoin cycle:

  ```bash
  scripts/leave-node.sh manupa@192.168.1.92
  scripts/cleanup.sh --check --node manupa@192.168.1.92    # laptop section should be all ✓
  scripts/join-node.sh manupa@192.168.1.92                  # back in the cluster
  ```
