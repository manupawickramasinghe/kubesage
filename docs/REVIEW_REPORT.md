# KubeSage Project Review Report

**Updated:** 29 October 2026, Asia/Colombo. Read-only live observations: approximately 16:52–16:58.

**Scope:** UI/UX, FinOps, Demo, Lab, agent team, GitOps, metrics/alerts, terminal/chat views, test cases,
deployment and cleanup. This replaces the earlier 29 October assessment of `c2d9481`.

## 1. Assessment

The project has progressed since the previous report. The merged application now has a dedicated Demo view,
a separate Agent team view, a dashboard split into maintainable source files, and a fix for empty provider
answers. The pending PR stack adds policy checks, fixes many earlier failures, and introduces a side terminal
and an agent hand-off chat.

Fresh isolated verification passed **124 pytest tests and 22 Chromium scenarios on merged main**, and
**141 tests and 37 Chromium scenarios on the pending stack**. Both full pytest runs included local Kustomize
checks; neither had skipped tests.

The earlier report's blanket statement that all twenty findings remain unresolved is now obsolete. Most
original reproductions have a corresponding fix in the pending stack. However, that stack is not merged,
and further probes found incomplete action-policy enforcement, capacity validation, ring concurrency and
cleanup recovery. The Demo also has evidence/status claims that can overstate success.

The latest saved live agent run observed today was **successful**, replacing the failed run as the latest
result. It produced a diagnosis and commit in 16 seconds for a recorded $0.019951. That measures team execution,
not alert-to-recovery time, and its record does not show an Apply. Historical failures remain relevant evidence.

This update changes documentation. It does not implement the remaining fixes or merge application PRs.

## 2. Revisions and PR status

Status was checked on 29 October. All findings and test results are pinned to these commits; a running server
is not assumed to have loaded the same Python revision as its worktree's current HEAD.

| Version | Revision | Status at review |
|---|---|---|
| Earlier report baseline | `c2d9481` | PR #13 merged; superseded as the review baseline |
| Merged application | `2425375` | [PR #15](https://github.com/manupawickramasinghe/kubesage/pull/15) merged into main |
| Finding fixes | `acff7b4` | [PR #16](https://github.com/manupawickramasinghe/kubesage/pull/16) open, based on main |
| Side terminal | `a2a9326` | [PR #17](https://github.com/manupawickramasinghe/kubesage/pull/17) open, stacked on #16 |
| Agent chat | `b098d29` | [PR #18](https://github.com/manupawickramasinghe/kubesage/pull/18) open, stacked on #17 |
| Latest reviewed candidate | `641c74b` | [PR #19](https://github.com/manupawickramasinghe/kubesage/pull/19) open, README changes stacked on #18 |

PRs #9, #10, #11 and #13 remain merged. Generated Executor PR #12 remains closed without merging, preserving
the faulty CPU-code test case. This report is maintained as Markdown; the earlier Word copy was deliberately
removed in a previous review.

**“Addressed in candidate” below means verified against `641c74b`, not shipped on main.** The pending order is
#16 → #17 → #18 → #19. The review-report PR is separate.

## 3. Product and UI/UX coverage

| Area | Merged main | Candidate additions or remaining boundary |
|---|---|---|
| Demo | Default entry view, pre-flight checks, nine-step run of show, narration/caption bar and live status | Useful presentation flow, but traffic, timing, cost and unknown-state copy need the corrections below |
| FinOps | Analyze, validation, Apply, traffic, immutable report history, exports, filters, plan review, live drift and readiness | Candidate fixes visible-plan/Apply alignment, sample coverage, inventory uncertainty and trace refresh |
| Dashboard source | `dashboard/` contains HTML, vendored fonts, CSS and classic scripts split by view; no CDN/build step | Assets are read on each request; Python changes still require a server restart |
| Agent team | Dedicated view, four collectors, Thinker/Executor, findings, diagnosis, diff, explicit Apply and opt-in PR creation | Candidate adds action/file binding, rendered checks, durable application state and clearer role outcomes |
| Under the hood | Five-stage pipeline, stage-filtered journal, allow-listed inspection shell and request/response Wire | Mock LLM mode and synthetic backfill remain supported; server-origin data is not automatically a production measurement |
| tmux | Read-only terminal mirror, windows, pause and download from the RAM ring | Candidate adds per-feed tails, pane zoom and a resizable right panel beside other views |
| Agent chat | Not present on merged main | Candidate builds hand-offs from saved run data, with run selection, links to agent reports and available reasoning excerpts |
| Responsive/accessibility | Light/dark themes, reduced-motion handling and responsive layouts | Candidate includes keyboard fixes and side-panel layouts; green browser scenarios are not a full accessibility audit |

The chat is a presentation of recorded findings, actions and outcomes. It is not an independent transport
between agents or a complete reasoning record. Reasoning excerpts depend on retained ring records and their
agent/run time windows. The RAM ring can wrap and does not survive reboot.

The terminal streams are read-only over HTTP. Start/Stop testcase, Run agents and Apply buttons are mutating
controls. PR creation requires `--agent-prs`; automatic alert-triggered team runs and cluster Apply are distinct.

## 4. Functionality and advanced components

- **FinOps:** deterministic statistics feed triage and tier routing, resource/scaling recommendations,
  guardrails, manifests and SKU estimates. Sizing uses scheduled peaks; optional `demand_burst` checks CPU
  autoscaler ceilings separately. Candidate first-fit-decreasing packing repairs the previous fragmented-pod
  example. It still does not model every scheduling constraint or shared-machine service budget.
- **Incident response:** the PromQL, resources, alerts and security collectors gather signals in parallel;
  the Thinker links diagnoses/actions to evidence; the Executor edits a separate Git clone. Syntax/build and
  policy checks filter changes but do not establish that arbitrary generated code is correct.
- **Policy/GitOps:** candidate action IDs and file scopes prevent several earlier bypasses; rendered
  namespace/kind/resource checks run before commit and Apply. Remaining semantic and capacity gaps are F01.
  Terraform checks remain heuristic/catalog-based, and web Apply skips Terraform.
- **Lab cases:** memory, throttling, CPU code, disk I/O, disk full, latency, Pending pods, pod density and an
  insecure unmanaged pod deliberately cause faults. Individual limits do not enforce a combined host budget.
- **Observability:** VictoriaMetrics, vmalert, Alertmanager, kubelet disk stats, request traces, journal and
  ring feeds are implemented. Automatic team runs default on, with a gathering delay and cooldown. Candidate
  fixes resolved-alert fingerprint retention; this is not a general incident lifecycle controller.
- **Operations:** pinned local tools, two-machine joining, demo reset and cleanup are implemented. Ownership
  checks improve in #16, but remote-failure recovery remains incomplete through the `cleanup.sh --all` wrapper.

## 5. Fresh verification

Tests ran in separate detached checkouts. The merged-main suite was explicitly given a private ring; the
candidate fixture also isolates test output from the live ring. No paid model, live load or cluster write was
used by these checks.

| Check | Main `2425375` | Candidate `641c74b` | What it establishes |
|---|---:|---:|---|
| Full pytest | **124 passed**, 14.58 s | **141 passed**, 14.84 s | Deterministic regressions, temporary Git origins and local Kustomize rendering |
| Chromium browser smoke | **22/22** | **37/37** | Isolated mocked API flows; not real provider/cluster end-to-end testing |
| Python syntax | 69 files | 70 files | In-memory compilation of every tracked Python file |
| YAML/JSON parsing | 37 YAML, 1 JSON | 37 YAML, 1 JSON | Parseability, not API schema/runtime validity |
| JavaScript syntax | 7 files | 8 files | `node --check` on every tracked JavaScript file |
| Kustomize | 9 directories | 9 directories | Local build, no Apply |
| Shell syntax | 9 scripts | 9 scripts | Individual `bash -n`, not cleanup/uninstall execution |

Browser coverage includes the original dashboard/error/history/download/focus/theme/traffic scenarios, Demo
and phone Demo, agent team, terminal mirror and responsive 390/768/1400 px light/dark layouts. Candidate
coverage additionally includes two side panels, per-feed tails, chat history/live state/untrusted text,
visible-plan/Apply alignment, cross-view errors, keyboard behavior, Wire refresh and unknown usage.

Additional deterministic probes confirmed both fixes and residuals listed below. Native Firefox interaction,
screen readers, a complete accessibility audit, provider repeatability and a new live throughput envelope
were not established. Earlier Firefox snapshots and three-node/32-pod fixtures remain historical evidence.

No Analyze/team run, traffic generation, backfill, Apply, reset, service restart, remote uninstall or teardown
was initiated for this report update. Existing services and workloads were left running.

## 6. Current read-only live snapshot

Observed around **16:52–16:58, 29 October 2026, Asia/Colombo** through bounded GET and kubectl reads.

| Component | Observation |
|---|---|
| Cluster | Desktop `k3d-kubesage-server-0` and laptop `manupa-hnx` both Ready; pool labels D8s_v5 and F8s_v2 |
| Demo | Five Deployments, eight Running/Ready pods; no demo HPA or traffic Job at inspection |
| Lab | No pods, Deployments or ScaledObjects; no active test cases or reported lab alerts |
| Metrics/alerts | VictoriaMetrics and Alertmanager health endpoints returned 200; Lab reported 24 series, newest sample about 3 seconds old |
| Latest FinOps report | Live run `20261029-114842-131212`, five workloads, recorded model cost **$0.011073** |
| Proposed pool | One F8s_v2, **$246.74/month** versus configured $280.32/month before: modelled difference **$33.58/month** |
| Plan versus cluster | `partial`, zero of five workloads fully matched, not ready; one required F8s_v2 node was Ready and available |
| Agent configuration | Automatic alert runs on; PR creation off; 12 saved runs; no pending auto trigger reported |
| Latest saved agent run | `20261029-034845`, live, `alert:ContainerOOMKilled`, **done**, all six roles done |
| Agent output | Four issues, four actions, three changed files; commit `159d7b4`; **16 seconds** from team start to finish, recorded **$0.019951** |
| Application of that output | `executor.applied` was null; a successful diagnosis/commit is not evidence of cluster recovery |
| RAM ring | 500 MiB capacity, roughly 4.9 MiB used, about 51,600 records, not wrapped at inspection |

This supersedes the earlier snapshot with an active ledger-cache fault, missing F4 capacity and a failed
latest team run. The older failed run `20261028-233415` is still historical evidence of the missing-content
bug; its old status is not a fresh failure of the new client code.

The server process was running from `/home/manupa/kubesage-agents`. It was not restarted to establish a
loaded-backend revision. Frontend files can change on reload independently of that Python process.

## 7. Reassessment of the original findings

“Open on main” refers to `2425375`. “Addressed” closes the cited reproduction within the reviewed candidate,
not every possible failure in that area. Residual details follow the table.

| ID | Original problem | Merged main | Candidate `641c74b` |
|---|---|---|---|
| F01 | Executor action/resource/namespace policy | Open | **Partial:** action/file binding and rendered boundaries added; unapproved fields/objects and capacity gaps remain |
| F02 | Old visible plan, new Apply ID after history failure | Open | **Addressed:** new plan rendered before fallible history refresh; browser regression passes |
| F03 | Positive replica cuts undercut aggregate CPU demand | Open | **Partial:** original 4-pod reproduction repaired; hard-cap edge still accepted below demand |
| F04 | Cleanup disconnects unrelated network attachments | Open | **Addressed:** detach requires KubeSage compose ownership; unknown attachments/network are retained |
| F05 | Missing provider content causes KeyError | **Addressed** in #15 | Original crash repaired; refusal classification added; malformed envelope types remain a separate residual |
| F06 | Limited ring reads skip retained records | Open | **Partial:** pagination repaired; concurrent new-segment cursor race remains |
| F07 | Obsolete autoscaling survives transitions/case stop | Open | **Partial:** fixed-plan managed deletion and named case cleanup added; orphan/renamed-object gaps remain |
| F08 | Aggregate-only node count misses pod fragmentation | Open | **Addressed example:** four 2-core pods now need four D4 nodes; first-fit packing remains an estimate |
| F09 | Missing samples/inventory look like free capacity | Open | **Partial:** original coverage/inventory reproductions addressed; completed Job pods can blank active totals |
| F10 | Overall done despite Executor failure; ready without Deployment | Open | **Addressed:** role-derived outcome and required target Deployment checks |
| F11 | Current-run Apply state lost on restart | Open | **Addressed:** atomic persistence for current and saved runs; matching-current branch independently probed |
| F12 | Errors appear only in a hidden view | Open | **Addressed reproductions:** shared toast exposes cross-view errors |
| F13 | Shell Tab trap and agent focus loss | Open | **Addressed reproductions:** native traversal and agent-card focus restoration |
| F14 | Wire remains stale after actions | Open | **Addressed reproductions:** operation invalidation/history refresh, preserving explicit selection |
| F15 | Image allowlist accepts prefix lookalikes | Open | **Addressed:** parsed repository/registry boundaries replace raw prefix matching |
| F16 | Unbounded Git/gh; brittle spike Job parsing | Open | **Addressed paths:** subprocess deadlines, managed label and safe metadata parsing |
| F17 | Exported report omits burst/clamp evidence | Open | **Addressed:** pool notes/burst warning exported; previously silent bounds now logged |
| F18 | Resolved/refiring alerts never become fresh | Open | **Addressed lifecycle:** resolved fingerprints removed, refire can be fresh subject to cooldown |
| F19 | Unbounded load queue and missing queue latency | Open | **Addressed source path:** bounded in-flight work, drops/queue time, cancellation/drain deadline; no fresh live load run |
| F20 | Remote uninstall reports success after failure | Open | **Partial:** direct leave fails correctly; `cleanup --all` can still erase its retained recovery record |

The earlier P3 second-resolution spike-name collision and Escape tooltip dismissal also have candidate fixes.
PR #17 additionally isolates test logging from the live ring and initializes quiet terminal feeds from their
own tails rather than one noisy shared tail.

## 8. Remaining and newly identified findings

Line references in this section refer to candidate **`641c74b`**, unless stated otherwise. P1/P2/P3 are review
priorities, not CVSS scores.

### F01 · P1 · Approved action does not constrain the complete semantic diff

`kubesage/agents/team.py:433–468` binds edits to action IDs and files, then compares only requested resource
keys for `set_resources`. An approved memory-limit change can also introduce an unrelated image, privileged
container and hostPath mount in the same allowed file. The binding, approval comparison and rendered policy
all returned no problems in an isolated probe. An additional unrelated Deployment in the rendered lab bundle
also passed. Namespace and kind checks improve containment but do not establish action-level authorization.

`kubesage/agents/policy.py:37–45,101–105` checks containers individually and makes capacity optional when
inventory is absent. Two containers requesting six cores each passed against an eight-core node; a 1 TiB
limit passed without node inventory. CPU and memory maxima can also come from different nodes. Require
validated inventory, compute pod-level scheduling demand against real eligible nodes, and compare the entire
diff/object set with explicitly approved changes. Human review of generated code remains necessary.

### F03 · P2 · Replica cap can leave the guarded plan below observed demand

`kubesage/recommend/guardrails.py:124,150` caps required replicas at 20. The original four-pod/0.8-core example
now has enough aggregate capacity. A separate 100-pod/20-core example still returned a 20-replica plan capped
at five cores. Surface infeasibility and raise resources or refuse the plan rather than presenting a capped,
insufficient result as covering observed demand.

### F05 · P2 · Malformed provider envelope types still escape classified handling

The original missing-content crash is fixed in merged main and the candidate. Separate candidate probes with
`choices: [null]` or list-valued `message.content` still raised `AttributeError` at
`kubesage/llm/client.py:161–167`. Validate envelope/container/content types before accessing them and retain
exchanges in the classified failure path. These were mocked responses, not newly observed provider failures.

### F06 · P2 · Ring cursor can advance past a concurrently added segment

`kubesage/ring.py:166–182` snapshots segment membership before reading the global next-record header. A
writer can add a segment between those snapshots. A deterministic interleaving returned records 1–2 with
cursor 3, although record 3 was in the omitted new segment; the next read returned nothing. The static
pagination regression is fixed. Advance only over a consistently scanned snapshot and test rollover during
reads, not just between completed reads.

### F07 · P2 · Case cleanup still depends on names and live pods

The candidate removes expected-name ScaledObjects/PDBs on a direct case stop, but it does not inventory every
Executor-created object by ownership. `kubesage/web/team_api.py:157` skips stop-all cases absent from pod-based
status. A case with only an orphan autoscaler/PDB can therefore be skipped, and differently named generated
objects are not comprehensively removed. Track all generated resources and clean them independently of
whether a pod currently exists. This residual is source-derived; no live case was started or removed.

### F09 · P2 · Terminal Job pods can suppress otherwise complete active usage

The candidate correctly refuses to display partial samples as spare capacity. However,
`dashboard/js/finops.js:346` excludes only Terminating pods, and `dashboard/js/core.js:14–16` requires a sample
for every remaining pod. A mocked completed load-generator pod plus eight fully sampled running pods made
the aggregate unknown, with “8 of 9 pods sampled”. The completed Job's 600-second TTL can prolong this state
(`kubesage/live.py:23`). Define the active-pod population explicitly and handle Succeeded/Failed pods without
hiding valid active-workload measurements.

### F20 · P2 · Full cleanup can discard a failed remote node's recovery record

`scripts/leave-node.sh` now propagates remote failures and checks uninstall postconditions. However,
`scripts/cleanup.sh:180` warns and continues after that failure, then `--all` removes the entire `.bin/`
directory at line 213, including the retained joined-node registry. Preserve failed-node records outside
removed paths or abort destructive local completion until recovery details are secured. No cleanup or remote
uninstall was executed for this finding.

### F21 · P2 · Demo can present incomplete or unhealthy evidence as success

`dashboard/js/demo.js:84–87` treats every completed traffic Job as done, ignoring `failed_requests` and
unavailable logs; missing success counts become zero. This disagrees with the more careful FinOps traffic
result. The guided proof must distinguish Job completion, known request errors and unknown results.

The same view needs more precise evidence labels:

- Lines 194–197 call team start-to-finish duration **“Alert to fix”**. The automatic path separately waits
  45 seconds to gather alerts (`kubesage/web/team_api.py:79–87`), and no Apply/recovery measurement is included.
- Lines 216–218 can label unknown plan/inventory state as the oversized “before” state. Unknown should remain
  unknown, rather than count as a successful pre-flight check.
- Lines 192–193 clamp negative savings to zero, hiding increased modelled pool cost on the entry view.

These behaviors are in merged PR #15 and remain in the candidate. The successful 16-second saved team run
should be described as **team execution to diagnosis/commit**, with application and recovery reported separately.

### F22 · P3 · Pending README repeats guarantees stronger than the implementation

PR #19's `README.md:114–115` again claims a bad answer cannot OOM-kill a pod. Historical resource floors cannot
guarantee future demand, and F01/F03 remain. Line 58 generalizes “structured parameters, never raw YAML” to the
whole product, although the Executor returns exact text edits that may contain YAML and code. The same
line claims every change is committed, while FinOps can Apply directly, and says nothing reaches the cluster
until Apply, despite separate testcase/reset controls. Line 47 also uses alert-to-commit wording for a
16-second team-duration record. Qualify the guarantees and distinguish
FinOps parameter rendering, Executor edits, modelled prices, team runtime and recovery measurements before
publishing those claims. Pinned main also retains the older OOM guarantee at `README.md:29–30`; this
documentation branch qualifies that text. PR #19 repeats it and adds the broader workflow/timing claims.

### F23 · P2 · Both side panels can leave too little room for the main view

The independent panel limits in `dashboard/css/chat.css:2–5` and `dashboard/css/tmux.css:38–51` reserve up to
34vw and 60vw respectively; `dashboard/css/base.css:99–102` subtracts both. Mocked browser probes with both
panels enlarged left a main column of about **61 px at 1024 px** and **84 px at 1400 px**. Text wrapped into an
unusable vertical strip even without horizontal document overflow. This is a candidate-only regression.
Enforce a shared minimum content width or switch one/both panels to overlays based on remaining space.

## 9. Continuing design and verification limits

- No login or user authorization is implemented. `X-KubeSage` is a browser cross-origin safeguard, not
  authentication. The observed service was listening on `0.0.0.0:8600`.
- FinOps, team and testcase operations have separate process-local locks, not one global/distributed transaction.
- Configured provider key values are redacted, and trace Authorization uses environment placeholders.
  Arbitrary terminal/source/log secrets are not comprehensively scrubbed; traces and chat can expose operational data.
- Historical metrics mix synthetic backfill with a live tail. Backfill replaces namespace history, including live data.
- Catalog prices and the configured before-pool are a model, not invoices. The separate demand-burst estimate
  does not provision nodes. Packing does not model every affinity, topology, storage, pod-overhead or host-service constraint.
- Model-call ledger values are not reconciled with provider billing, particularly for failures/retries.
- YAML parsing, compilation and Kustomize rendering do not prove safe behavior. Terraform is neither cloud-validated
  nor applied by the web action. Successful diagnosis/commit is not successful remediation.

## 10. Historical live evidence

These observations were recorded in earlier guides/runs and were not replayed for this update.

| Evidence | Recorded result and boundary |
|---|---|
| 27 October FinOps | Run `20261027-222729-780339`, six calls, $0.011127; 11 validation and 11 Apply steps succeeded; reload recovered the applied state |
| 80 requests/s, four-replica ceiling | checkout-web 3 → 4; sampled CPU 124% versus 70% target; intervals with 12–35 errors and roughly 7–11-second p95 |
| Later 60 requests/s | Initial 23 failures; after six replicas, recorded intervals had zero errors and 170–240 ms p95 |
| 60 requests/s on laptop | 3 → 4; first-minute four failures and up to nine-second p95; later zero-error intervals with 330–410 ms p95 |
| Agent CPU-code case | Reported thread-to-process change: about 1.0 → 2.99 cores, 2.4M → 6.4M transactions/s; generated PR #12 closed unmerged |
| Multi-incident agent case | Four issues/five actions in about 15 seconds for $0.035, one recorded run |
| Latency case | Client p95 reportedly 29 seconds while application histogram stayed below 0.5 seconds |
| Cleanup | Earlier throwaway-cluster teardown; laptop leave/rejoin not freshly exercised |

Short runs and selected zero-error intervals do not establish an SLO or a sustainable throughput envelope.
PR #19's additional 2 → 7 replica/~110 ms result was not independently remeasured for this review.

## 11. Recommended next work and reproduction

1. Complete F01's semantic authorization and fail-closed, pod-level capacity checks before relying on broader
   automated writes. Add adversarial regression cases for the accepted changes above.
2. Repair F03 infeasibility handling, F06 concurrent rollover, F07 ownership-based cleanup and F20 failed-node recovery.
3. Correct malformed provider envelopes, terminal-pod usage and the two-panel minimum width. Fix Demo
   success/unknown/timing/cost handling and pending README claims so presentation agrees with detailed views.
4. Re-run focused regressions after those changes. Then perform a deliberately bounded live rehearsal, checking
   application, rollout, client errors/latency and host headroom separately; add Firefox and accessibility coverage.
5. Review the stacked PRs in dependency order. Passing tests and this report do not substitute for review of the
   final generated changes. No application PR was merged for this review.

To reproduce the deterministic checks from the chosen source checkout:

```bash
# Use an environment installed from this checkout; pinned .bin/kubectl enables all local rendering tests.
review_ring_dir=$(mktemp -d)
KUBESAGE_RING="$review_ring_dir/ring" KUBESAGE_RING_MB=4 .venv/bin/python -m pytest -q
.venv/bin/python tests/browser_smoke.py --executable /path/to/chromium --output-dir /tmp/kubesage-browser
```

The browser script needs Playwright/Chromium and intercepts API traffic with fixtures. No live test cases are
started by it. Source review and targeted probes are not a formal proof of all possible behaviors.

| Area | Sources on merged main; candidate adds files where stated |
|---|---|
| UI | [dashboard/](../dashboard), [Demo](../dashboard/js/demo.js), [FinOps](../dashboard/js/finops.js), [agents](../dashboard/js/agents.js), [Lab](../dashboard/js/lab.js), [tmux](../dashboard/js/tmux.js); candidate adds `dashboard/js/chat.js` |
| API/shell | [server.py](../kubesage/web/server.py), [team_api.py](../kubesage/web/team_api.py), [shell.py](../kubesage/web/shell.py) |
| FinOps | [pipeline.py](../kubesage/pipeline.py), [recommend/](../kubesage/recommend), [metrics/](../kubesage/metrics), [LLM client](../kubesage/llm/client.py), [apply.py](../kubesage/apply.py), [gitops.py](../kubesage/gitops.py) |
| Agents/policy | [team.py](../kubesage/agents/team.py), [edits.py](../kubesage/agents/edits.py), [definitions](../agents), [application sources](../gitops); candidate adds `kubesage/agents/policy.py` |
| Observability/cases | [ring.py](../kubesage/ring.py), [journal.py](../kubesage/journal.py), [kubestats.py](../kubesage/kubestats.py), [lab/](../kubesage/lab), [alerts](../deploy/alerts) |
| Operations/tests | [scripts/](../scripts), [Compose](../docker-compose.yml), [tests/](../tests), [browser suite](../tests/browser_smoke.py) |

Supporting guides: [UI](ui-guide.md), [agents](agents.md), [API](api.md), [testing](testing.md),
[cleanup](cleanup.md), [historical UI review](ui-review.md). Screenshots in those guides illustrate recorded
states; the pinned tests and timestamped observations above define this review's evidence.
