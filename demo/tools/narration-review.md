# How the narration was written and checked

This is the third version of the voice-over (4 November 2026). It replays the FinOps analysis of 1 November on the Meta
Muse models, with the business-calendar forecast, and drops the flash-sale replay of earlier versions.

1. **Draft** (Claude): one voice-over paragraph per scene, sized so the whole video stays under three minutes and the
   website part under one, from the recorded data (`data/report.json`, run `20261101-211354-839221`; `data/agents.json`,
   run `20261029-034845`) and the code.
2. **Clarity edit** (agy, model `gemini-4-argon-high`): asked to edit only for flow and listenability, keep every fact
   and number, and make no scene longer. It split a few sentences; no numbers changed.
3. **Fact-check, round 1** (Claude, against the code and `data/`): five wordings corrected (below).
4. **Fact-check, round 2** (agy, read-only, with file:line evidence): told that the cluster is hypothetical and the
   website part simulated, and asked to check every claim about how KubeSage works against the code and docs and every
   number against `data/`, the new forecast and calendar claims hardest. Codex, which checked the earlier versions,
   could not run this time: its login had expired.
5. **Final** ([narration.json](narration.json)): every correction applied to the voice-over and to the slides.

## What was corrected

| scene | draft said | evidence | the video now says |
|---|---|---|---|
| summary | forecasts the rush from your sales calendar | `kubesage/forecast.py`: the forecast combines the calendar with 30 days of usage; report-worker's comes from history alone | "forecasts rushes from usage and your calendar" |
| server | two services reserve two cores and use almost none | `report.json` stats: report-worker uses 1.74 cores during its 02:00 batch | "sit mostly idle" |
| rush | Black Friday gets a one-day scale-up | `report.json`: the scale-up is a follow-up in the recorded plan, not applied by Apply | "the plan asks for a one-day scale-up from seven thirty" |
| finops | only the hard case goes to Muse Wonder | `report.json` ledger: Wonder also chose the node pool | "Muse Wonder takes the hard case and the node pool" |
| finops | guardrails restore any pre-warm a model drops | `kubesage/recommend/guardrails.py` `_forecast_windows`: weekly calendar windows only | "restore dropped calendar pre-warms" |
| d-analyze | seventy percent less reserved capacity | `kubesage/pipeline.py`, `costing.py`: $168.73 → $50.65 is a time-averaged cost; peak reserved CPU rises | "seventy percent less reservation cost" |
| d-analyze | the node pool grows thirty-two percent, to fit every planned peak | `report.json` node_pool: the cost grows 32%; the pool is sized for scheduled (weekly) peaks, not dated ones | "To fit scheduled peaks, the node pool costs thirty-two percent more" |
| d-analyze | the forecast lists each rush | `dashboard/js/finops.js`: the panel shows each workload's strongest window | "each service's biggest rush" |
| d-apply | pods move to the new pool once its nodes exist | `kubesage/apply.py`: the migrations are skipped; Apply must run again once the nodes exist | "It skips moving pods until the new nodes exist" |

## What the check confirmed

- **The calendar and the forecast:** `config/calendar.yaml` declares working hours, Black Friday (3×), Christmas week and
  New Year; Stage 1.5 (`kubesage/forecast.py`) turns them and the 30-day statistics into pre-warm times and pod counts at
  a 70% CPU target, with no model call. The recorded forecast pre-warms checkout-web to 8 pods at 08:45 on weekdays and
  plans Black Friday from 07:30.
- **The CPU trigger:** every workload that doesn't scale to zero gets a 70% CPU trigger in the recorded plan.
- **The models and the cost:** one `muse-spark-1.3` triage call, Spark for the four routine workloads, `muse-wonder-1.0`
  for auth-api and the node pool: $0.265931, against $0.586656 with every call on Wonder.
- **The node pool:** 1× D8s_v5 $280.32 → 3× F4s_v2 $370.11 a month, "sized for scheduled peaks".
- **Evidence or it's dropped:** findings without a valid collected signal ID are dropped (`kubesage/agents/team.py`).
- **The team run:** 16 seconds, $0.0200, 4 issues, 4 actions, 3 changed files, commit `159d7b4d`.
