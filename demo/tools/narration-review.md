# How the narration was written and checked

1. **Draft** (Claude): one voice-over paragraph per scene, sized so the whole video stays under three minutes and the
   website part under one, from the recorded data and the docs.
2. **Clarity edit** (agy, model `gemini-4-argon-high`): asked to edit only for flow and listenability, keep every
   fact and number, and make no scene longer. It split long sentences and trimmed a few words; no numbers changed.
3. **Fact-check** (Codex CLI 0.164, `codex exec --sandbox read-only` against this branch): told that the cluster is
   hypothetical and the website part simulated from recorded data, and asked to check every statement about how
   KubeSage works against the code and docs, and every number against `demo/tools/data/`, with file:line evidence,
   without changing anything.
4. **Final** ([narration.json](narration.json)): agy's wording, with every Codex correction below applied, to the
   voice-over and to the slides.

## What Codex corrected

| scene | draft said | evidence | the video now says |
|---|---|---|---|
| summary | every change is a git commit a person reviews | `kubesage/web/server.py`, `dashboard/js/agents.js`: FinOps Apply writes to the cluster directly; agent fixes are commits | "Agent fixes become git commits that a person reviews before applying"; the slide says nothing reaches the cluster until a person says so |
| server | requests were set high; even busy hours use under two cores | `report.json` stats: checkout-web alone peaks at 4.66 cores; auth-api's memory request is below its median | "Some requests were set high, just in case: two services reserve two cores each and use almost none" (payment-service 0.18 and report-worker 0.01 cores at p95) |
| finops | guardrails keep requests above measured usage | `kubesage/recommend/guardrails.py`: floors come from medians or active-period statistics | "Guardrails floor requests from history and keep scaling within bounds" |
| team | a policy check rejects any edit the plan did not ask for (after the commit) | `kubesage/agents/policy.py`, `edits.py`, `team.py`: checks limit each edit's files and fields by action type, before the commit | "Policy checks limit each edit's scope and type, before the commit"; the diagram puts the check before the commit |
| d-analyze | finds thirty-three dollars a month on this one node | `report.json` node_pool: $280.32 → $246.74 compares one D8s_v5 with one F8s_v2 | "a cheaper node would save about thirty-four dollars a month, twelve percent" |

Codex confirmed the rest, including about one cent for the analysis, and 16 seconds, $0.0200, two security actions and
three changed files for the team run.
