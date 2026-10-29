# How the narration was written and checked

1. **Draft** (Claude): one voice-over paragraph per scene, from the recorded data and the docs.
2. **Clarity edit** (agy, model `gemini-4-argon-high`): asked to edit only for flow and listenability, keep every
   fact and number, and stay within 15% of each scene's length. It split long sentences and smoothed transitions;
   no numbers changed.
3. **Fact-check** (Codex CLI 0.164, `codex exec --sandbox read-only` against `main` at `788e3bb`): asked to check
   every statement against the code and docs, with file:line evidence, without changing anything.
4. **Final** ([narration.json](narration.json)): agy's wording, with every Codex correction below applied. Two of
   the dashboard's own Demo captions made the same "bigger model" claim and were corrected in
   `dashboard/js/demo.js` too.

## What Codex corrected

| scene | draft said | evidence | the video now says |
|---|---|---|---|
| cluster | money spent on unused capacity | `docs/ui-guide.md`: prices are a model of cloud VMs | each machine stands in for a priced cloud VM; "every **demo** pod" |
| analyze | thirty days of usage; hard cases reach a bigger model | `docs/HANDOVER.md`, `config/models.cerebras.yaml`: synthetic backfill plus live samples; all tiers are `gpt-oss-120b` at low, medium and high reasoning effort | "a synthetic backfill, plus live samples"; "only the harder ones get more reasoning" |
| decided | healthy workloads stop after free statistics; medium and large models; guardrails check every number | `docs/pipeline.md`, `kubesage/recommend/guardrails.py` | healthy stop after triage; medium and high reasoning; guardrails hold requests above measured usage and keep scaling in bounds |
| apply | Apply moves the pods | `docs/testing.md`: a real Apply moved them on 28 October; the latest plan had not been applied | "as a real Apply did on the twenty-eighth of October" |
| traffic | 23 requests failed while pods started | `docs/testing.md`: a separate 27 October run; failures counted in sampled intervals; p95 170–240 ms | names the recorded run, "the sampled intervals showed twenty-three failed requests", "one hundred seventy to two hundred forty milliseconds" |
| break | loads 700 MB; the team starts by itself | `ledger_cache.py`, `deploy/alerts/rules.yml`, `kubesage/web/team_api.py` | "tries to hold"; "the alert rule fires"; the live 45 s wait; "the run that follows is the real one, recorded for this alert" |
| investigate | each chat message is a real hand-off | `dashboard/js/chat.js`: the chat is rebuilt from the run's saved reports | "the chat rebuilds this run's reports and plan as hand-offs" |
| ship | every edit is checked; a memory-optimised node pool | `kubesage/agents/team.py`, `kubesage/web/team_api.py`: Terraform is a proposal, never applied by the dashboard | "a proposal for a memory-optimised node pool, which needs a separate Terraform apply"; "the Kubernetes changes pass policy checks" |
| why | nothing is trusted without evidence; every fix is a commit a person approves | `kubesage/agents/team.py`, `README.md` | "this incident cost about two cents"; "every finding must cite collected evidence"; "agent fixes become commits that a person reviews" |

Codex confirmed the numbers as given: $33.58 a month, about one cent, 16 seconds and $0.0200 for the team run.
