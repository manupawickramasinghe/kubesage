# KubeSage UI/UX and Functionality Review Report

**Date:** 27 October 2026 (Asia/Colombo)  
**Repository:** `manupawickramasinghe/kubesage`  
**Implementation:** `b128153`, branch `feat/control-center-ux`  
**Pull request:** [PR #10](https://github.com/manupawickramasinghe/kubesage/pull/10) — open and unmerged when this report was prepared  
**Word document:** [Download this report](reports/KubeSage-Review-Report-2026-10-27.docx)

## 1. Outcome

The Control Center now presents a clearer sequence: analyze usage, review and validate a plan, apply it,
then examine live scaling and request health. Historical analysis values are explicitly separated from live
Kubernetes readings. Application status is reconstructed from cluster objects instead of relying on a
browser-session flag.

The implementation also adds useful review controls and corrects functional issues in capacity sizing,
schedule costing, replica guardrails, report persistence and command failure handling. Validation recorded
**73 passing automated tests**, **17 passing browser scenarios**, and a real two-machine demo run.

The live traffic test confirmed autoscaling but also exposed saturation and HTTP request failures at the
replica ceiling. A traffic health panel was added so the dashboard shows that distinction.

## 2. Scope and constraints

The review followed [HANDOVER.md](HANDOVER.md) and covered the single-file web interface, HTTP API,
recommendation guardrails, cost calculations, manifest application and analysis persistence. The demo uses
the desktop control-plane machine and laptop worker, with Cerebras for live model calls.

The existing delivery constraints were preserved: vendored Inter and Instrument Serif fonts, one script,
no CDN, no frontend build step, light/dark themes, reduced-motion support and escaped external text.
Cluster testing ended with a demo reset. Other services and the protected repositories were outside scope.

## 3. Findings and changes

| Finding | Delivered change |
|---|---|
| Historical requests were labelled “Reserved now” | Charts say “Reserved at analysis”; current requests appear in the live inspector |
| Applied state disappeared after reload | Deployment and ScaledObject fields are compared with the latest analysis; rollout readiness is shown separately |
| Validation and partial operations could look complete | “Validate only” has explicit semantics; failed and skipped steps remain visible |
| Errors were easy to miss at the bottom of the page | Action results and failures appear near the controls as well as in Activity |
| Polling removed keyboard focus | Diagram, legend and inspector updates preserve focus; SVG focus styles and tooltip associations were added |
| Mobile charts and ledger values were hard to read | Compact charts separate labels from bars; ledger numbers stay on one line; layouts were checked at three widths |
| Traffic controls omitted scaling context | Current/max replicas, CPU target, estimated CPU work and replica-limit explanations are visible |
| A completed load Job could hide failed HTTP requests | Traffic results show successful/failed requests and latest p50/p95 latency; missing results remain unknown |
| CPU-driven maximum replicas were omitted from node demand | Pool sizing includes the configured CPU autoscaler ceiling |
| Cron costs could double-count overlapping targets | Costing uses the highest active target and accounts for overnight/weekend schedules |
| Replica bounds and invalid schedules could escape guardrails | Bounds, scaling headroom and accepted cron windows are checked before rendering |
| Apply could continue after a failed command | Commands have timeouts; dependent remaining steps are skipped after failure |
| Reports could be observed before all files were written | Completed runs are published atomically, cannot overwrite existing runs, and use more precise IDs |

## 4. Advanced functionality delivered

- **Report history:** choose completed analyses while live cluster polling continues.
- **Pinned writes:** Apply and GitOps use the selected run ID; namespace mismatches are rejected.
- **Exports:** download a JSON report, all manifests, or a workload's manifests.
- **Recommendation filters:** search by workload and filter by status or proposed changes.
- **Plan review:** inspect machine placement, pricing rationale, guardrail adjustments and migration manifests.
- **Live drift inspection:** inspect differing fields between live objects and the latest plan.
- **Rollout and placement checks:** distinguish matching specs from a ready rollout; skip migrations when
  there are too few Ready, uncordoned nodes carrying the target SKU label.
- **Traffic observability:** rediscover active jobs after reload and display numeric request results from
  bounded, cached log reads, including results from completed or failed jobs.
- **API reliability:** validate request bodies and parameter types, serialize mutations within the web
  server instance, bound subprocesses and redact configured credential values from JSON responses.

## 5. Validation evidence

| Validation | Recorded result | Coverage limit |
|---|---|---|
| Python automated suite | 73 tests passed | Deterministic tests; no live model calls |
| Isolated browser suite | 17 scenarios passed | Chromium with mocked API responses |
| Responsive themes | 390, 768 and 1400 px, light and dark | Visual and interaction checks at these widths |
| Larger cluster layout | Three machines and 32 pods | Synthetic layout stress case |
| Firefox 155 | Settled DOM rendering snapshots at all three widths and both themes | Interactive regression coverage used Chromium |
| Accessibility interactions | Focus preservation, SVG focus, tooltip labels, keyboard selection, reduced motion | Not a formal screen-reader or WCAG certification audit |
| Untrusted text | Model/node text escaping checked | Does not replace a full security audit |
| Credentials | Tracked/staged files checked before implementation push | Secrets remain in the ignored local environment file |

The browser scenarios cover empty/offline states, failed operations, validation versus application,
reload/reset behavior, history-pinned actions, download contents, filters, theme persistence, keyboard
focus and traffic results. They explicitly check that a completed Job with request failures remains
visibly marked and that unavailable logs do not imply zero errors.

## 6. Real-cluster demo

| Step | Observation |
|---|---|
| Starting state | Eight original workload pods on the desktop; none on the laptop |
| Analyze | Live Cerebras run `20261027-222729-780339`: six calls costing $0.011127 |
| Proposed placement | One F8s_v2-labelled machine; configured model cost $246.74/month versus $280.32/month before |
| Validate only | 11/11 steps checked, no skips |
| Apply | 11/11 commands succeeded; planned workload placement/specs matched and rollout became ready |
| Browser reload | Applied state was recovered from Kubernetes |
| Traffic | One run at 80 requests/second, 50 ms CPU work/request, for 180 seconds |
| Autoscaling | checkout-web increased from three to four replicas; sampled CPU reached 124% against a 70% target |
| Metrics failure | Brief VictoriaMetrics outage produced a readable Analyze 502; the service was restored |
| Networking | A laptop test pod resolved cluster DNS and reached the checkout Service |
| Final state | Demo reset: both machines Ready, eight original desktop pods, no active traffic jobs |

The $33.58/month difference is a **configured price-model estimate**. It is not a measured billing reduction
and does not imply that moving pods switches off a machine.

### Traffic finding

The heavy traffic run reached the four-replica limit and produced HTTP errors and multi-second p95 latency.
Observed ten-second log intervals included 12–35 failed requests, with p95 roughly 7–11 seconds. These are
interval observations, not a complete request-failure total for the run.

This verified scaling and saturation behavior; it did not establish error-free throughput. The traffic
health panel was implemented after this finding. Its parser and UI states were validated with deterministic
backend/browser tests; the heavy load was not repeated. Request counters sum available interval samples;
displayed latency describes the latest interval.

## 7. Remaining limits

- **Historical verification:** live drift/application checks compare the latest report in the configured
  namespace. Older selected reports have pinned actions but no historical live-verification badge.
- **Modelled evidence:** usage history combines synthetic backfill and live metrics. Prices come from the
  configured cloud cost model.
- **Placement assurance:** counting Ready, uncordoned, SKU-labelled nodes is not a complete scheduler or
  free-capacity guarantee. Taints, competing workloads and placement constraints still matter.
- **Operation coordination:** mutation serialization belongs to one web-server instance; it is not a
  distributed lock across separate CLI/server processes.
- **Access control:** the existing LAN access model remains. `X-KubeSage` prevents cross-origin browser
  POSTs; it does not authenticate a user.
- **Traffic evidence:** a countdown, increased CPU or a completed Job cannot establish successful requests.
  Results depend on available job logs and the configured replica/machine limits.
- **Review status:** changes are on the feature branch in PR #10; this report does not claim they are merged
  or production-certified.

## 8. Proposed follow-up priorities

These items are proposed future work, not completed features:

1. Add authentication/authorization before widening access beyond the current trusted environment.
2. Make live verification selectable by historical run ID.
3. Extend placement checks to account for pod constraints and existing resource demand.
4. Persist request-health summaries beyond job-log retention and demo resets.
5. Establish explicit latency/error-rate goals and test lower traffic levels to measure sustainable throughput.
6. Add native Firefox interaction and screen-reader coverage.

## 9. Files and reproduction

| Area | Main files |
|---|---|
| Dashboard | `kubesage/web/static/index.html` |
| HTTP API and cluster/traffic state | `kubesage/web/server.py` |
| Applying and traffic startup | `kubesage/apply.py`, `kubesage/cli.py`, `kubesage/live.py` |
| Guardrails and costing | `kubesage/recommend/guardrails.py`, `cron.py`, `costing.py` |
| Analysis persistence | `kubesage/pipeline.py`, `kubesage/service.py` |
| Regression coverage | `tests/test_web.py`, `test_apply.py`, `test_pipeline.py`, `test_service.py`, `browser_smoke.py` |

Run the automated suite without using the cluster or spending LLM credit:

```bash
.venv/bin/python -m pytest -q
```

Run the optional browser suite with Playwright and an installed Chromium browser:

```bash
.venv/bin/python tests/browser_smoke.py --executable /path/to/chromium --output-dir /tmp/kubesage-browser
```

This report records the completed implementation's validation; preparing the document did not rerun live
analysis or traffic tests. See the [UI guide](ui-guide.md), [API reference](api.md), [testing guide](testing.md)
and [short review with screenshots](ui-review.md) for supporting detail.

Screenshots: [desktop](images/ui-control-center.png) · [phone, dark theme](images/ui-control-center-phone.png).
