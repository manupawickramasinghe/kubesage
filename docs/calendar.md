# Business calendar and forecasting

KubeSage scales **before** a rush, not during it — and it never lets a model invent the rush.

## Why a calendar?

Metrics history can only show what already happened. It cannot see next month's Black Friday, a
Christmas shopping week, or a one-off promotion, and an LLM asked to guess those dates will hallucinate
them. So future business events live in one operator-edited file, `config/calendar.yaml`, which is the
source of truth:

- the **deterministic forecast** (Stage 1.5, $0, no LLM) turns each event plus the metrics into an
  expected load, a pre-warm time and a replica count, with a stated confidence;
- the **LLM explains** the forecast and proposes the KEDA plan in business language;
- **guardrails enforce** it: a weekly calendar window the model forgot is added back, starting at the
  pre-warm time, with room to reach the forecast replica count;
- **costing quantifies** it in dollars *and* percent, per workload and for the node pool.

## Declaring an event

```yaml
prewarm_minutes: 15   # default lead time when an event does not set its own

events:
  # Weekly: fits the KEDA cron subset, so it can be applied as a trigger.
  - name: Weekday working hours
    kind: working-hours          # working-hours | sale | holiday | promotion | other
    days: "1-5"                  # cron day-of-week, Sunday=0
    start_time: "09:00"
    end_time: "17:00"
    traffic_multiplier: 1.8      # expected load vs normal (off-peak) load
    prewarm_minutes: 15          # scale up this long *before* 09:00
    workloads: [checkout-web]    # names, or ["*"] for every workload

  # Dated: forecast and priced, but a weekly cron cannot express a one-off date.
  - name: Black Friday sale
    kind: sale
    date: "2026-11-27"
    repeat_yearly: true
    start_time: "08:00"
    end_time: "22:00"
    traffic_multiplier: 3.0
    prewarm_minutes: 30
    cooldown_minutes: 60
    workloads: [checkout-web, payment-service]
```

A yearly date range uses `start_date`/`end_date` as `"MM-DD"` (the Christmas peak ships as
`12-20` to `12-26`). Exactly one of `days`, `date` or `start_date`/`end_date` per event; the loader
rejects anything else.

The shipped calendar declares weekday working hours (pre-warm 08:45, scale down after 17:00), Black
Friday (3×), the Christmas shopping week (2.2×) and a New Year sale (2×). Edit the multipliers to match
your own expectations — they are estimates you own, not numbers the model made up.

## How the forecast is computed (`kubesage/forecast.py`)

For each workload, from Stage 1 stats and the calendar:

- **baseline** — median hourly total CPU: normal load;
- **history windows** — each recurring peak becomes a window starting `prewarm_minutes` early; confidence
  is the peak's recurrence discounted by how much history backs it;
- **calendar windows** — expected load is `baseline × traffic_multiplier`, floored at any peak the
  metrics already prove (a sale day still contains the normal rush); declared events score 0.8–0.85
  confidence because the operator vouched for the timing;
- **replicas needed** — `ceil(expected CPU ÷ (CPU request × 70% target))`, plus the off-peak count that
  covers the baseline: the scale-down target outside the window.

A declared event is also a **price question**: holding peak replicas 24/7 is the waste, so scheduling
them only around the window is where the saving comes from. Reports state it as
`$280.32 → $246.74/mo · saves 12.0%` — and, honestly, as `costs 32.0%` when a forecast plan buys more
capacity than the cluster has today.

## Weekly vs dated events

Weekly events render as KEDA cron triggers and guardrails enforce them. Dated events (a specific sale
day, a holiday week) cannot be a weekly cron, so KubeSage does not fake one: the recommendation covers
them with `max_replicas` headroom plus a CPU trigger, and carries a follow-up — *"Before 2026-11-27
08:00: apply a timed scale-up…"* — for the operator or a future timed ScaledObject.

## What the models may and may not do

Prompts give triage and remediation the forecast JSON and instruct them to cite the event name, the
expected multiplier and the pre-warm time — and to **never invent, move or rename a calendar event**.
If a rush is not in the calendar and not in the metrics, KubeSage will not predict it. Add it to
`config/calendar.yaml` instead.
