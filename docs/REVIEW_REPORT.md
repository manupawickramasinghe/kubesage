# KubeSage Project Review Report

**Updated:** 29 October 2026, Asia/Colombo

**Reviewed revision:** `c2d9481`, the merge of PR #13.

**Scope:** UI/UX, FinOps pipeline, Lab, agent team, GitOps, observability, test cases, deployment and cleanup.

**Status:** PRs [#9](https://github.com/manupawickramasinghe/kubesage/pull/9),
[#10](https://github.com/manupawickramasinghe/kubesage/pull/10),
[#11](https://github.com/manupawickramasinghe/kubesage/pull/11) and
[#13](https://github.com/manupawickramasinghe/kubesage/pull/13) are merged. This report supersedes the
27 October assessment. Markdown is the maintained report format; the earlier Word copy was deliberately
removed in the subsequent code review.

## 1. Assessment

KubeSage now contains two workflows: historical FinOps optimization and an incident-oriented agent team.
The web interface exposes them through the FinOps dashboard, Architecture & CLI Lab, and tmux view. The
agent team collects evidence, produces diagnoses and prepares edits in a separate Git clone. Operators can
review those changes, optionally open a PR, and explicitly apply application manifests.

Fresh verification on the merged revision passed **119 pytest tests** and **20 isolated Chromium browser
scenarios**. Static checks also passed for **68 Python files, 37 YAML files, one JSON file, nine Kustomize
directories and all nine shell scripts**.

Passing regression tests do not establish complete safety or readiness. Additional source review and
isolated reproductions found unresolved issues, including:

- Executor edits are structurally validated but are not bound to approved action policies or a hard `lab`
  namespace boundary.
- An Analyze/history-refresh failure can leave an old plan visible while Apply submits the new run.
- Replica reductions can preserve per-pod floors while cutting aggregate capacity below observed demand.
- Ring polling can skip retained records; several status, error-visibility and keyboard flows are incomplete.
- The latest saved live team run observed during this review was **failed**, with `KeyError: 'content'`.

The current result is a substantial demonstrable prototype with identified follow-up work. It should not
be described as guaranteeing that every generated change is safe to apply. This update changes documentation;
the findings below remain open in the reviewed application code.

## 2. Changes since the previous report

| Change | Current behavior |
|---|---|
| PR #10 merged, including its follow-up review | FinOps history, downloads, filters, plan review, traffic results and live comparison are on main |
| `0e58780` review fixes | Narrower configured-key redaction, inherited run-directory permissions, generation-aware cluster caching, refresh queuing and parsed-plan caching |
| `54b1493` sizing policy | Pool size uses scheduled peaks; CPU autoscaler ceilings are calculated separately as `demand_burst` and checked for fit/shortfall |
| Traffic default | Dashboard default is **60 requests/s × 50 ms**, about three cores of requested CPU work; the separate CLI spike default remains 40 requests/s |
| PR #9 merged | Cleanup, joined-node tracking, remote leave/uninstall and operational documentation |
| PR #11 merged | Lab pipeline view, journal, allow-listed inspection shell, request traces and deliberate history backfill |
| PR #13 merged | Four collectors, Thinker/Executor, custom rules, nine lab cases, alert automation, RAM ring and tmux |
| Report status | The old “PR #10 open/unmerged” statement and 73-test headline are obsolete |

Earlier claims that pool sizing reserves capacity for every CPU autoscaler at its maximum are also obsolete.
The current implementation sizes scheduled peaks and warns separately about maximum scale-out demand. A
warning does not provision capacity or prevent every future Pending pod.

## 3. Current functional coverage

| Area | Implemented | Practical boundary |
|---|---|---|
| FinOps dashboard | Analyze, Validate only, Apply, traffic, run history, exports, workload filters, plan details, live inspector | Snapshot values stay fixed; live comparison verifies the latest report in the configured namespace |
| FinOps pipeline | Deterministic statistics, triage/routing, resource/scaling parameters, guardrails, manifests, allocation and pool estimates | Floors reflect observed history; positive replica cuts and scheduler packing remain incomplete |
| Lab | Five-stage pipeline, live metrics, stage-filtered journal, shell/history/completion and wire inspector | Backfill replaces namespace history, including live samples; it is a data mutation |
| Inspection shell | Allow-listed kubectl reads, PromQL and KubeSage summaries; argument-list execution; bounded output | It is not an arbitrary terminal; namespace overrides and all-namespace reads are allowed for approved resources |
| Agent team | PromQL, resources, alerts and security collectors; evidence-linked findings; Thinker diagnosis; Executor changes | Evidence references and syntax checks do not establish semantic correctness or enforce every platform rule |
| Executor/GitOps | Separate clone and branch, edit matching/re-indentation, YAML/Python/basic Terraform checks, Kustomize build, labelled fallback edits, opt-in PRs | Human review remains necessary; Terraform is skipped by cluster Apply |
| Lab cases | Memory, CPU throttling, CPU code, disk I/O, disk full, latency, Pending pods, pod density and an insecure unmanaged pod | They intentionally create faults and resource load; they are not normal healthy application examples |
| Alerts/metrics | vmalert, Alertmanager, additional workload series and kubelet emptyDir/filesystem export | Automatic team runs default on; repeated-alert detection and shared-machine headroom are limited |
| tmux/ring | Read-only stream mirror, filtering, pause, bounded RAM retention and text download; local operator-shell output can be mirrored | Ring reads are read-only, but toolbar case/team buttons perform POST actions; cursor correctness needs repair |
| Operations | Local pinned tools, real-machine joining, reset and cleanup | Remote uninstall was not freshly exercised; destructive cleanup is not fully isolated from unrelated network attachments |

Agent definitions and plain-English rules are loaded from `agents/`. Those rules influence prompts; they
are not a deterministic policy engine. Agent PR creation is opt-in through the server's `--agent-prs`
configuration. Team execution, PR creation and cluster application are distinct actions.

## 4. Fresh verification on 29 October

| Check | Result | What it establishes |
|---|---|---|
| `pytest -q` | **119 passed**, 14.49 seconds | Existing deterministic regression coverage, including a mock team run against a temporary Git origin |
| `tests/browser_smoke.py` | **20/20 passed** | Mocked dashboard flows, team panel, tmux mirror and responsive cases |
| Dashboard themes/layout | 390, 768 and 1400 px, light/dark | Six checked layouts; not arbitrary device coverage |
| Additional Lab/tmux fixtures | Twelve view/width/theme combinations, no document overflow | Visual containment for these views; does not establish keyboard correctness |
| Python syntax | 68 tracked files compiled in memory | Syntax only; application source was not executed by this check |
| YAML/JSON parsing | 37 YAML files and one JSON file parsed | Parseability, not Kubernetes schema or runtime correctness |
| Kustomize | Nine tracked application/demo directories built successfully | Local rendering; no manifests were applied |
| Shell syntax | All nine scripts passed individual `bash -n` checks | Syntax only; cleanup behavior was not executed |
| Additional targeted probes | Confirmed failures listed below using mocked HTTP, in-memory data or source inspection | Failure cases outside the existing green suite |

The browser suite uses isolated API fixtures. It covers validation/application state, errors, history-pinned
requests, download contents, filters, focus, theme persistence, untrusted text, traffic outcomes, the agent
panel and tmux. Additional probes exposed failures that its current twenty scenarios do not test.

Firefox rendering snapshots and the three-machine/32-pod stress layout belong to the earlier review. They
were **not freshly repeated** against this revision. Native Firefox interaction, screen readers and a formal
accessibility/security audit are not established by these results.

No paid Analyze/team run, traffic generation, backfill, Apply, reset, remote uninstall or teardown was
initiated for this update. The existing server and workloads were left running.

## 5. Read-only live snapshot

Observed around **01:45 on 29 October 2026, Asia/Colombo**. These observations describe the running instance,
not a new end-to-end demonstration or a guarantee about every deployment.

| Component | Observation |
|---|---|
| Machines | Desktop and laptop both Ready; labels D8s_v5 and F8s_v2 |
| Demo namespace | Five Deployments, eight Running/Ready pods on the desktop; no demo traffic Jobs or HPA shown |
| Lab namespace | Memory case active: one ledger-cache pod on the laptop, 25 recorded restarts at inspection |
| Metrics/alerting | VictoriaMetrics and Alertmanager health endpoints answered; Lab reported metrics available with a roughly 14-second sample age |
| Latest FinOps report | Live run `20261028-233212-950994`, five workloads, recorded LLM cost $0.010985 |
| Latest proposed pool | Two F4s_v2-labelled nodes, modelled $246.74/month; the real cluster has **zero** Ready nodes carrying that label |
| Live plan | Pending, zero of five workload plans matched; required target capacity unavailable |
| Burst note | That report's modelled burst demand was about 6.25 cores versus 7.20 configured allocatable cores; this does not resolve the real label mismatch |
| Agent configuration | Six roles present, Cerebras profile, automatic alert runs on, agent PR creation off |
| Latest saved team run | `20261028-233415`, live, **failed**, recorded cost $0.011826, error `KeyError: 'content'` |
| Failed-run child states | Four collectors done; Thinker still labelled running and Executor queued in the saved result |
| RAM ring | 500 MiB capacity, approximately 1.7 MiB occupied, 16,234 records, not wrapped at inspection |

ledger-cache is an intentional fault case; its restarts should not be mistaken for evidence that the demo
namespace is failing. The failed team run is a real unresolved result, and should not be hidden by historical
successful demonstrations. The record does not establish the provider-side reason for the missing content.

The inspected server was running from the separate `kubesage-agents` worktree. This review did not restart it
or prove a loaded-process revision identifier; code/tests are explicitly pinned to `c2d9481`.

## 6. Historical live evidence, kept separate

The following results were recorded before this review. Sources are the previous review, [testing guide](testing.md),
[agent guide](agents.md) and merged PR descriptions. They were not replayed or independently remeasured today.

| Recorded run | Result and limit |
|---|---|
| FinOps Analyze, 27 October | Run `20261027-222729-780339`: six live calls, $0.011127; 11 validation and 11 Apply steps succeeded; reload recovered the applied state |
| 80 requests/s, four-replica ceiling | checkout-web scaled 3 → 4; sampled CPU reached 124% against a 70% target; observed intervals had 12–35 errors and roughly 7–11-second p95 |
| Later 60 requests/s run | Initial scaling transient had 23 recorded failures; after six replicas, recorded intervals had zero errors and 170–240 ms p95 |
| 60 requests/s on laptop | Scaled 3 → 4; first minute recorded four failures and up to nine-second p95; later intervals had zero errors and 330–410 ms p95 |
| Agent CPU-code case | Thread-pool-to-process-pool change reportedly increased use from about 1.0 to 2.99 cores and throughput from 2.4M to 6.4M transactions/s; generated PR #12 was closed unmerged |
| Multi-incident agent demonstration | Four issues/five actions recorded in about 15 seconds for $0.035; this is one run, not a repeatability guarantee |
| Latency case | Client-side p95 reportedly reached 29 seconds while the application's histogram stayed below 0.5 seconds, motivating the client probe |
| Cleanup | Earlier throwaway-cluster teardown was recorded; laptop leave/rejoin remained explicitly untested |

The original $33.58/month difference is a configured price-model estimate, not measured billing savings.
Short runs and selected zero-error intervals do not establish an SLO or sustainable throughput envelope.
Recorded LLM ledger values are not an independent reconciliation of provider billing, especially for failed
or retried calls.

## 7. Unresolved findings

Priorities below are review priorities, not CVSS scores. **P1** should be addressed before trusting broader
automation or shared-machine cleanup. **P2** affects correctness, reliability or accessibility. **P3** is a
smaller hardening/usability issue. Source line references refer to reviewed revision `c2d9481`.

### P1 — policy, review and operational boundaries

**F01 · Executor policy checks do not cover the final rendered change.**
`kubesage/agents/team.py:481` materializes model edits without binding every edit to an approved action ID,
target and resulting policy. `kubesage/agents/edits.py:73–92` validates structure; a `1Ti` memory limit passed the
isolated validator probe. Thinker checks also accepted a negative CPU request, memory request above limit
and an unknown namespace in separate in-memory probes (`kubesage/agents/team.py:318–377`). Agent Apply runs
`kubectl apply -k/-f` without validating the rendered namespace/kind boundary (`kubesage/web/team_api.py:259–272`);
parseable out-of-lab Namespace YAML passed validation. Kubernetes may reject some invalid values, but this
does not enforce the intended platform policy. Bind edits to approved actions and validate final objects,
resources, namespace/kind allowlists and platform constraints before commit and Apply.

**F02 · Apply can submit a plan the user has not seen.**
`kubesage/web/static/index.html:1663–1665` switches internal run/report before awaiting `/api/reports`. If that request
fails, the displayed report stays old while Apply at line 1673 uses the new ID. Reproduction displayed
`20260102` but submitted `20260103` to a mocked Apply endpoint. Render/commit selected-run state atomically;
a history refresh failure should not detach the visible plan from the submitted plan.

**F03 · Positive replica reductions can violate observed aggregate demand.**
`kubesage/recommend/guardrails.py:46–55` calculates a replica floor only for proposed scale-to-zero. Per-pod floors at
lines 84–98 do not protect a positive reduction. A four-pod workload using 0.8 total CPU was accepted as one
pod limited to 0.25 CPU, without a replica warning. Check combined replicas and resources against measured
aggregate demand for every scaling recommendation.

**F04 · Cleanup can modify unrelated network attachments.**
`scripts/cleanup.sh:167–171` disconnects every container attached to the cluster network unless it has the
matching k3d cluster label; it does not require KubeSage ownership. This is narrower than stopping unrelated
containers, but still changes their networking and contradicts the documented “never touched” boundary.
Use explicit ownership checks and refuse unknown attachments. This was found by source inspection; no
cleanup command was executed.

### P2 — reliability, evidence and UX

| ID | Finding and evidence | Required follow-up |
|---|---|---|
| F05 | **Provider-response handling can crash a live run.** Latest saved run failed with `KeyError: 'content'`; `llm/client.py:150` indexes content without checking the response envelope. A mocked missing-content response reproduced the same exception. | Validate the envelope, classify empty/refusal/truncated responses, preserve diagnostics and finish child states consistently |
| F06 | **Ring cursors skip retained records.** `ring.py:162–177` returns the newest global sequence when a limited read stops early. Five records with limit two returned records 1–2 and cursor 5; the next read was empty. Browser/CLI advance to that cursor. | Return a continuation cursor for the last delivered/scanned record and test pagination with stream filters and concurrent writers |
| F07 | **Obsolete autoscaling survives transitions.** FinOps fixed-replica rendering does not delete a previous managed ScaledObject. `lab/__init__.py:120–132` deletes the original case definition and source ConfigMaps, missing an Executor-added ScaledObject. | Track ownership/generated objects and remove obsolete autoscalers during transitions and case stop |
| F08 | **Pool fitting is an aggregate estimate.** `recommend/costing.py:65–74` checks totals/largest pod, not packing. Four two-core pods produced a three-node D4 candidate with 3.6 allocatable cores/node, although four nodes are required. | Preserve pod shapes and simulate placement; include constraints, existing demand and non-Kubernetes headroom |
| F09 | **Missing data resembles spare capacity or empty inventory.** UI lines 852/1344 sum partial samples or show missing usage as zero; one of eight samples displayed 3% utilization without coverage. `web/server.py:607/712` can return empty objects with `available: true` after inventory reads fail. | Carry sample coverage and inventory availability separately, show unknown values and gate dependent actions |
| F10 | **Success/readiness can be false.** Executor preparation/model failures mark that role failed but `agents/team.py:276/456–478` can finish the overall run as done. `web/server.py:533–536` can mark a scaling-only plan ready when its target Deployment is absent. | Derive outcome from all required components and require the target workload to exist and be observed ready |
| F11 | **Agent Apply status is not durable for the current run.** `web/team_api.py:282–285` writes run.json only when the applied run differs from `current`; ordinary current-run Apply status can disappear after restart. | Persist current-run application outcomes atomically |
| F12 | **Errors can be hidden in another view.** Generic errors go to dashboard-only notice/activity (`index.html:956`); team/case errors go to Lab-only `#team-msg` (`2223`). Lab Analyze failure and tmux case-start failure were reproduced with invisible messages. | Use a shared visible announcement/result region or an error region in each active view |
| F13 | **Keyboard access is incomplete outside the dashboard.** Shell Tab/Shift+Tab remains trapped when input has text (`index.html:2443`). Agent-card re-rendering loses focus because restoration omits `data-agent` (`909/2114`). | Preserve native focus traversal, separate completion from navigation and restore focus within the active view |
| F14 | **Wire can remain stale after actions.** `index.html:1861–1863` reloads only on run-ID change. Mocked same-run Apply and new Analyze left the loaded trace/run selector stale beyond polling. | Refresh history and invalidate the relevant trace after operations, while preserving selection |
| F15 | **Image allowlisting accepts prefix lookalikes.** `agents/security/collect.py:58` uses `startswith`; probes accepted `python-malicious:1` and `registry.k8s.io.attacker/payload:1`. | Parse image registry/repository identity and require exact or slash-delimited matches |
| F16 | **Some failure paths remain unbounded or brittle.** Legacy `gitops.py:33/94` subprocesses lack timeouts and can hold a mutation lock. `web/server.py:695–703` trusts a spike-name prefix and parses SECONDS directly; an unrelated malformed Job made cluster reads fail in a mocked probe. | Bound Git/gh operations; validate ownership and safely parse observed workload metadata |
| F17 | **Exported evidence omits some safety changes.** `summary.py:52–56` omits pool notes/burst warnings from GitOps REPORT.md. `guardrails.py:78–82` silently adjusts some replica/CPU-target bounds; a probe returned no notes for those changes. | Preserve burst warnings in every output and record every applied clamp |
| F18 | **Repeated alerts may not retrigger automation.** `web/team_api.py:39/64–66` never clears the fingerprint set when an alert resolves. A later firing with the same fingerprint is no longer fresh. | Track alert lifecycle, clear resolved entries and test resolve/refire with cooldown |
| F19 | **Load duration and latency are incomplete under backlog.** `lab/loadgen.py:60–74` queues work without backpressure/cancellation; executor shutdown can outlive SECONDS. Timing at line 22 excludes time waiting in the executor queue. This is source-derived, not a new live load result. | Bound outstanding work, record queue delay/drops and cancel/drain against an explicit deadline |
| F20 | **Remote uninstall can report success after failure.** `scripts/leave-node.sh:20–37` uses remote `set -u` without fail-fast, suppresses uninstall output, prints success and then removes the joined-node record. | Check uninstall results and postconditions before reporting success/removing the record; test safely on an isolated host |

### P3 and cross-cutting limits

- `live.py:15` uses second-resolution spike Job names: two accepted starts in one second can collide.
- `index.html:1657` dismisses tooltips on Escape only when a selection exists; an unselected focused chart
  tooltip remained visible in a fixture check.
- There is no login. The custom POST header is a cross-origin browser safeguard, not user authentication.
- FinOps, team and testcase operations have separate locks. They are not one global or cross-process
  transaction, and overlapping groups are not universally rejected with 409.
- Configured LLM key values are redacted; arbitrary terminal/log secrets are not comprehensively scrubbed.
  Authorization in LLM traces is symbolic, but traces still contain source, prompts and operational data.
- The RAM ring overwrites history and does not survive reboot; job logs and demo resets also limit history.
- Terraform validation checks syntax heuristics/catalog membership, not `terraform validate/plan` or cloud
  deployment. Terraform is not applied by the web action.
- The modelled before-pool is configured, not discovered from actual cloud invoices. Scheduled-average
  workload costs exclude unpredictable CPU scale-out and do not include scheduler fragmentation.

## 8. Recommended sequence

1. Resolve F01–F04 before broadening automated writes or relying on cleanup on shared machines. Add
   regression cases that fail against the reviewed revision and enforce the intended boundaries.
2. Diagnose the observed failed live run (F05), repair ring pagination and success/persistence reporting
   (F06/F10/F11), then repeat a deliberately bounded live demonstration when authorized for that work.
3. Make inventory/metrics uncertainty and errors visible in every view; fix keyboard traversal and Wire
   invalidation (F09/F12–F14).
4. Make testcase cleanup repeatable, preserve burst/clamp evidence, and bound backlog/external commands
   (F07/F16–F20).
5. Define latency/error and shared-machine headroom targets, measure an end-to-end throughput envelope,
   and add native Firefox and screen-reader coverage.

These are proposed fixes, not completed work in this documentation update. Human review remains necessary
for model-authored changes; successful parsing/building or a green test suite is not an approval policy.

## 9. Source map and reproduction

| Area | Sources |
|---|---|
| Web, shell and team controls | [server.py](../kubesage/web/server.py), [shell.py](../kubesage/web/shell.py), [team_api.py](../kubesage/web/team_api.py), [index.html](../kubesage/web/static/index.html) |
| FinOps pipeline | [pipeline.py](../kubesage/pipeline.py), [service.py](../kubesage/service.py), [recommend](../kubesage/recommend), [metrics](../kubesage/metrics), [LLM client](../kubesage/llm/client.py) |
| Agent implementation and definitions | [team.py](../kubesage/agents/team.py), [edits.py](../kubesage/agents/edits.py), [agents/](../agents), [GitOps application sources](../gitops) |
| Observability and live cases | [ring.py](../kubesage/ring.py), [journal.py](../kubesage/journal.py), [kubestats.py](../kubesage/kubestats.py), [lab/](../kubesage/lab), [alert rules](../deploy/alerts) |
| Operations | [scripts/](../scripts), [Compose](../docker-compose.yml), [cleanup guide](cleanup.md), [cluster guide](cluster.md) |
| Tests | [tests/](../tests), [browser smoke script](../tests/browser_smoke.py) |

Reproduce the existing deterministic suites without a paid model call or live case:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python tests/browser_smoke.py --executable /path/to/chromium --output-dir /tmp/kubesage-browser
```

The second command needs Playwright and Chromium. It mocks API traffic; it does not start the nine real lab
cases. Source review covered the listed components and the recorded failure probes, not a formal proof of
all possible behaviors. No newly found application issue was silently marked fixed.

Supporting material: [API](api.md), [UI guide](ui-guide.md), [agent guide](agents.md), [testing](testing.md),
and the [historical 27 October UI review](ui-review.md). Existing screenshots illustrate the shipped views:
[dashboard](images/ui-control-center.png), [Lab](images/ui-lab.png), [agent team](images/ui-agents.png),
[tmux](images/ui-tmux.png) and [phone dashboard](images/ui-control-center-phone.png).
