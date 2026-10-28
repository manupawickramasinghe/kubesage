# agents/: the KubeSage agent team

Each folder with an `agent.yaml` is one agent. The engine in `kubesage/agents/` loads whatever is here, so you
can add an agent, change a model or add a rule without touching the engine.

```
sub-agents (small model)                 Thinker (large model)        Executor (medium model)
  promql     VictoriaMetrics   ─┐
  resources  Kubernetes API    ─┼─ compact findings ─▶ issues + actions ─▶ edits under gitops/ ─▶ pull request
  alerts     Alertmanager      ─┤        ▲                    ▲
  security   posture + web log ─┘        └──── rules/*.txt ───┘
```

| file | what |
|---|---|
| `models.yaml` | Model profiles (`cerebras`, `meta`) and what `small`, `medium` and `large` mean in each |
| `rules/*.txt` | Custom rule sets in plain English, given verbatim to the Thinker and the Executor |
| `<agent>/agent.yaml` | `name`, `role` (`subagent`, `thinker` or `executor`), `model` size, `settings`, `enabled` |
| `<agent>/prompt.md` | The agent's instructions. The engine appends the JSON contract for its role |
| `<agent>/collect.py` | Sub-agents only: `collect(ctx)` reads live data and records signals with `ctx.signal(...)` |

## Add a sub-agent

1. Copy a folder, e.g. `cp -r agents/alerts agents/network`, and set `name: network` in its `agent.yaml`.
2. Write `collect(ctx)` in `collect.py`. It can call `ctx.query(promql)`, `ctx.kube("get", "pods", "-n", "lab")`,
   `ctx.kube_raw(path)`, `ctx.http_json(url)` and `ctx.access_log()`; all of them are read-only. Record facts
   with `ctx.signal(id, kind, target, value, unit=, detail=, severity=)`. Set `severity` only when your
   own threshold says something is wrong.
3. Describe what the agent looks for in `prompt.md`.
4. Restart `kubesage serve`. The new agent shows up in the Lab's agent team and gets its own tmux pane.

Set `enabled: false` to take an agent out of the team without deleting it.

## Change a model

- One agent: edit `model:` in its `agent.yaml` (`small`, `medium` or `large`). You can also add
  `overrides: {extra_body: {reasoning_effort: low}}`.
- A whole size: edit that size's entry in `models.yaml`.
- The provider: set `KUBESAGE_MODELS=cerebras` (or `meta`) before `kubesage serve`.

## What each role must return

The engine validates every reply. A finding must cite signal ids the agent actually collected. An action must
name an issue and a real target. An edit must match its file exactly once and leave the file valid (YAML parses,
`kubectl kustomize` builds, Python compiles, Terraform SKUs exist in `config/skus.yaml`). Anything that fails is
dropped and listed in the run, never silently kept. The schemas are in `kubesage/agents/schemas.py`.
