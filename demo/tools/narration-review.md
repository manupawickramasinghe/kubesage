# How the narration was written and checked

1. **Draft** (Claude): one voice-over paragraph per scene, sized so the whole video stays under three minutes and the
   website part under one, from the recorded data and the docs. This version leads with what is new: FinOps and SRE in
   one system, event-driven scaling for sale-day rushes, runs that cost cents, evidence or it's dropped, and a person in
   charge.
2. **Clarity edit** (agy, model `gemini-4-argon-high`): asked to edit only for flow and listenability, keep every
   fact and number, and make no scene longer. It split long sentences; no numbers changed.
3. **Fact-check** (Codex CLI 0.164, `codex exec --sandbox read-only` against this branch): told that the cluster is
   hypothetical and the website part simulated from recorded data, and asked to check every statement about how
   KubeSage works against the code and docs, and every number against `demo/tools/data/`, with file:line evidence,
   without changing anything. The novelty and scaling claims were checked hardest.
4. **Final** ([narration.json](narration.json)): agy's wording, with every Codex correction applied to the voice-over
   and to the slides.

## What Codex corrected

| scene | draft said | evidence | the video now says |
|---|---|---|---|
| summary | FinOps and SRE in one loop | `docs/agents.md`: the FinOps analysis and the agent team are separate workflows | "in one system" (also on the slide and the close) |
| summary | it cuts the bill | `docs/ui-guide.md`, `kubesage/apply.py`: savings are modelled; Apply doesn't retire the old VM | "It plans the savings" |
| summary | a run costs cents | `report.json` $0.011073, `agents.json` $0.019951: two measured runs | "these runs cost cents" |
| summary | nothing changes until a person presses Apply | `kubesage/agents/team.py`: the commit comes first; Reset demo changes the cluster on its own | "Plans and fixes reach the cluster only when a person applies them" |
| d-analyze | a cheaper node that saves about $34 a month | `report.json` node_pool: $280.32 → $246.74 is an estimate | "Analyze estimates about thirty-four dollars in monthly savings" |

An earlier version had five more corrections that still hold: some requests (not all) were set high; guardrails floor
requests from history rather than keeping them above measured usage; policy checks limit each edit's scope and type
before the commit; agent fixes are commits that a person reviews; and the savings compare one D8s_v5 with one F8s_v2.

## What Codex confirmed

- **Built for the rush:** the recorded plan has 30-day statistics, a weekday 09:00–11:00 peak, a KEDA cron window from
  08:50 and a 70% CPU trigger. KubeSage's own schedules are weekly (`kubesage/recommend/cron.py`), so crediting the
  Black Friday window to engineers, who can add triggers to the rendered ScaledObject, is honest. The node pool is
  sized for the peaks KubeSage schedules, not for windows added later (`kubesage/recommend/costing.py`).
- **Evidence or it's dropped:** findings without a valid collected signal ID are dropped (`kubesage/agents/team.py`).
  This checks citations, not whether the signal proves the conclusion.
- **The flash sale:** 60 req/s and scaling out to six pods are documented in `docs/testing.md`.
- The numbers: $33.58 a month, about one cent for the analysis, and 16 seconds, $0.0200, two security actions and three
  changed files for the team run.
