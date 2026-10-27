# Two real machines: 8600G + 3500U as two node SKUs

`TWO_NODE=1 scripts/cluster-up.sh` fakes two SKUs with two k3d containers on one host. For the hackathon
pitch you can do the same thing with two physical machines, so judges watch pods move from the desktop to
the laptop. **This guide hasn't been run end to end yet.** Try it before demo day.

k3d runs Kubernetes inside Docker, which makes it awkward for a second physical machine to join. Use plain
[k3s](https://k3s.io) on both machines instead.

| machine | role | label (`kubesage.io/sku`) |
|---|---|---|
| Ryzen 5 8600G desktop | k3s server, runs KEDA + monitoring | `D8s_v5` (the "expensive" pool) |
| Ryzen 5 3500U laptop | k3s agent | `F4s_v2` (the "cheap" pool) |

Both machines need to reach each other on port 6443. Tailscale works if the venue network doesn't; use
the Tailscale IPs below.

## 1. Server (8600G)

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.36.4+k3s1 sh -s - server \
  --disable traefik --node-ip <8600G-ip> --tls-san <8600G-ip>
sudo cat /var/lib/rancher/k3s/server/node-token      # join token for the agent
sudo cat /etc/rancher/k3s/k3s.yaml > .bin/kubeconfig  # then replace 127.0.0.1 with <8600G-ip>
```

## 2. Agent (3500U)

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.36.4+k3s1 \
  K3S_URL=https://<8600G-ip>:6443 K3S_TOKEN=<token> sh -s - agent --node-ip <3500U-ip>
```

## 3. Label the nodes and deploy

```bash
export KUBECONFIG=$PWD/.bin/kubeconfig PATH=$PWD/.bin:$PATH
kubectl label --overwrite node <8600G-hostname> kubesage.io/sku=D8s_v5
kubectl label --overwrite node <3500U-hostname> kubesage.io/sku=F4s_v2
kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.21.0/keda-2.21.0.yaml
kubectl apply -k deploy/k8s/demo/
```

From there the demo is the same: `kubesage analyze`, then `kubesage apply`. The `*.migrate.yaml` patches move
every workload onto the laptop. `scripts/demo-reset.sh` moves them back.

If the node-pool step picks a SKU other than `F4s_v2`, relabel the laptop with that SKU or adjust
`config/skus.yaml`. `kubesage apply` skips migrations when no node has the target label, so nothing goes Pending.
