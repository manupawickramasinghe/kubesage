# gitops/: the desired state the agents change

This folder is the "application repo" of the KubeSage demo platform. The test cases deploy from it, and the
**Executor** agent's pull requests edit it.

| path | what |
|---|---|
| `apps/namespace.yaml` | The `lab` namespace that test cases run in |
| `apps/<app>/` | One workload per test case: `deployment.yaml`, its `kustomization.yaml`, and the app's source in `src/`. The source ships as a ConfigMap, so a code change rolls the pods. |
| `infra/nodepools.tf` | The AKS node pools (SKU, node count, max pods) that the home-lab machines stand in for |

Deploy an app the way the test cases do: `kubectl apply -f apps/namespace.yaml && kubectl apply -k apps/<app>`.

Merging an Executor PR fixes a test case for good: the next run of that test case starts from the fixed state.
To repeat a demo, close the PR instead of merging it, or revert the merge.
See [docs/agents.md](../docs/agents.md).
