"""kubesage simulate | stats | analyze | costs | budgets | apply | serve ..."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from rich.console import Console

from kubesage.config import load_skus

console = Console()


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--vm", default=os.environ.get("KUBESAGE_VM_URL", "http://localhost:8428"),
                   help="Prometheus/VictoriaMetrics base URL")
    p.add_argument("--namespace", default="demo", help="the namespace to read; several separated by commas")
    p.add_argument("-A", "--all-namespaces", dest="namespace", action="store_const", const="*",
                   help="every namespace except the shared ones in config/costs.yaml")
    p.add_argument("--tz", default=os.environ.get("KUBESAGE_TZ", "Asia/Colombo"), help="timezone for daily patterns")
    p.add_argument("--days", type=int, default=30)


def _cluster_wide(p: argparse.ArgumentParser) -> None:
    """Arguments of the commands that read every namespace."""
    p.add_argument("--vm", default=os.environ.get("KUBESAGE_VM_URL", "http://localhost:8428"),
                   help="Prometheus/VictoriaMetrics base URL")
    p.add_argument("--tz", default=os.environ.get("KUBESAGE_TZ", "Asia/Colombo"), help="timezone the days are labelled in")
    p.add_argument("--days", type=int, default=30)


def cmd_simulate(args: argparse.Namespace) -> None:
    from kubesage.simulate.backfill import generate, import_to_vm, to_jsonl
    from kubesage.simulate.scenarios import DEMO_WORKLOADS

    end = int(time.time()) // 60 * 60
    started = time.perf_counter()
    series = generate(DEMO_WORKLOADS, namespace=args.namespace, days=args.days, end=end, step=args.step,
                      tz=args.tz, seed=args.seed)
    if args.out:
        if args.fleet:
            from kubesage.simulate import fleet

            series = series + fleet.generate_fleet(days=args.days, end=end, tz=args.tz, seed=args.seed)
        Path(args.out).write_text("\n".join(to_jsonl(series)) + "\n")
        console.print(f"wrote {len(series)} series to {args.out}")
        return
    samples = import_to_vm(args.vm, series, namespace=args.namespace, replace=not args.keep_existing)
    console.print(f"backfilled {samples:,} samples ({len(series)} series, {args.days} days) into {args.vm} "
                  f"in {time.perf_counter() - started:.1f}s")
    for w in DEMO_WORKLOADS:
        console.print(f"  [cyan]{w.name:<16}[/] {w.story}")
    if args.fleet:
        from kubesage.simulate import fleet

        started = time.perf_counter()
        around = fleet.generate_fleet(days=args.days, end=end, tz=args.tz, seed=args.seed)
        samples = import_to_vm(args.vm, around, namespace=args.namespace, replace=not args.keep_existing,
                               delete_match=fleet.DELETE_MATCH)
        console.print(f"backfilled the rest of the cluster for `kubesage costs`: {samples:,} samples, "
                      f"{len(fleet.FLEET)} workloads in {', '.join(fleet.namespaces())} on {len(fleet.NODES)} nodes, "
                      f"in {time.perf_counter() - started:.1f}s")


def cmd_stats(args: argparse.Namespace) -> None:
    from kubesage.report import print_stats
    from kubesage.service import AnalysisError, collect_stats

    try:
        stats, _ = collect_stats(vm=args.vm, namespace=args.namespace, days=args.days, tz=args.tz)
    except AnalysisError as exc:
        sys.exit(str(exc))
    if args.json:
        print("[" + ",".join(s.model_dump_json() for s in stats) + "]")
    else:
        print_stats(console, stats)


def _cost_report(args: argparse.Namespace):
    from kubesage.config import load_costs
    from kubesage.costs import costs_now
    from kubesage.metrics.client import PromClient, PromError

    try:
        report = costs_now(vm=args.vm, days=args.days, tz=args.tz, end=time.time(), cfg=load_costs(),
                           catalog=load_skus(), client=PromClient(args.vm, journal=False))
    except PromError as exc:
        sys.exit(f"metrics query failed: {exc}")
    if not report.by_namespace:
        sys.exit(f"no pods found at {args.vm} in the last {args.days} days; run `kubesage simulate` first")
    return report


def cmd_costs(args: argparse.Namespace) -> None:
    from kubesage.report import print_costs

    report = _cost_report(args)
    if args.json:
        print(report.model_dump_json(indent=2))
    elif args.prometheus:
        from kubesage.costs import prometheus_text

        print(prometheus_text(report), end="")
    else:
        print_costs(console, report, by=args.by, top=args.top)


def cmd_budgets(args: argparse.Namespace) -> None:
    """Exit 1 when a budget is over, so a pipeline or a cron job can act on it."""
    report = _cost_report(args)
    if not report.budgets:
        sys.exit("no budgets configured: add some to config/costs.yaml")
    if args.json:
        print("[" + ",".join(b.model_dump_json() for b in report.budgets) + "]")
    else:
        for b in report.budgets:
            run_rate = "nothing ran" if b.monthly_usd is None else f"${b.monthly_usd:,.2f} of ${b.budget_usd:,.2f} ({b.used_pct:.0f}%)"
            style = {"over": "bold red", "at-risk": "yellow", "ok": "green", "unknown": "dim"}[b.status]
            console.print(f"  [{style}]{b.status:<8}[/] {b.scope:<9} {b.name:<16} {run_rate}")
    over = [b for b in report.budgets if b.status == "over"]
    if over:
        sys.exit(f"{len(over)} budget(s) over: {', '.join(f'{b.scope} {b.name}' for b in over)}")


def _stats(args: argparse.Namespace):
    from kubesage.service import AnalysisError, collect_stats

    try:
        return collect_stats(vm=args.vm, namespace=args.namespace, days=args.days, tz=args.tz)[0]
    except AnalysisError as exc:
        sys.exit(str(exc))


def cmd_whatif(args: argparse.Namespace) -> None:
    """What a different traffic multiplier would need and cost. Deterministic: no model call."""
    from rich.table import Table

    from kubesage.config import load_calendar
    from kubesage.whatif import simulate

    calendar = load_calendar()
    if args.event and args.event not in {e.name for e in calendar.events}:
        sys.exit(f"no event named {args.event!r} in config/calendar.yaml; it has: "
                 + ", ".join(repr(e.name) for e in calendar.events))
    rows = simulate(_stats(args), calendar, multiplier=args.multiplier, catalog=load_skus(), event_name=args.event)
    if args.json:
        print("[" + ",".join(r.model_dump_json() for r in rows) + "]")
        return
    if not rows:
        sys.exit("nothing to simulate: no calendar event or recurring peak touches these workloads")
    t = Table(title=f"What if traffic were {args.multiplier:g}× normal? ($0.00, no LLM)", title_justify="left")
    for col in ("workload", "event", "cores", "replicas", "held all month", "scheduled", "scheduling saves"):
        t.add_column(col, justify="left" if col in ("workload", "event") else "right", overflow="fold")
    for r in rows:
        saves = "—" if r.scheduled_savings_pct is None else f"{r.scheduled_savings_pct:.1f}%"
        t.add_row(r.workload, r.event, f"{r.baseline_total_cpu:.2f} → {r.expected_total_cpu:.2f}",
                  f"{r.replicas_now} → {r.replicas_needed}", f"${r.monthly_at_peak_usd:,.2f}",
                  f"${r.monthly_scheduled_usd:,.2f}", saves)
    console.print(t)
    for note in dict.fromkeys(r.note for r in rows if r.note):
        console.print(f"[dim]note: {note}[/]")


def cmd_calendar(args: argparse.Namespace) -> None:
    """Draft business-calendar events from the metrics or from an .ics file. Nothing is written without --approve."""
    import yaml

    from kubesage import config
    from kubesage.calendar_suggest import append_event, suggest_events

    path = config.CONFIG_DIR / "calendar.yaml"
    if args.action == "suggest":
        events, skipped = suggest_events(_stats(args), config.load_calendar()), []
    else:
        from kubesage.calendar_import import parse_ics

        if not args.file:
            sys.exit("usage: kubesage calendar import FILE.ics")
        try:
            events, skipped = parse_ics(Path(args.file).read_text(), default_multiplier=args.multiplier)
        except OSError as exc:
            sys.exit(f"cannot read {args.file}: {exc}")
    for reason in skipped:
        console.print(f"[yellow]skipped[/] {reason}")
    if not events:
        console.print("no events to add")
        return
    if args.approve:
        wanted = events if args.approve == ["all"] else [e for e in events if e.name in args.approve]
        missing = [] if args.approve == ["all"] else sorted(set(args.approve) - {e.name for e in events})
        if missing:
            sys.exit(f"no drafted event named: {', '.join(missing)}")
        for e in wanted:
            try:
                append_event(path, e, approved=True)
            except ValueError as exc:
                sys.exit(str(exc))
            console.print(f"[green]added[/] {e.name} to {path}")
        return
    print(yaml.safe_dump({"events": [e.model_dump(exclude_none=True) for e in events]}, sort_keys=False), end="")
    console.print(f"[dim]{len(events)} draft event(s); nothing was written. Review the multipliers, then re-run with "
                  f"--approve all, or --approve 'NAME' for one.[/]")


def cmd_realized(args: argparse.Namespace) -> None:
    """Did the saving an analysis promised show up? Compares the run's snapshot with the cluster now."""
    import json

    from rich.table import Table

    from kubesage import apply
    from kubesage.metrics.stats import WorkloadStats
    from kubesage.realized import realized_savings

    try:
        run_dir = Path(args.run) if args.run else apply.latest_run(args.out_dir)
        report = json.loads((run_dir / "report.json").read_text())
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"no analysis run to compare with: {exc}")
    args.namespace = report.get("namespace", args.namespace)
    before = [WorkloadStats.model_validate(s) for s in report["stats"]]
    predicted = {w["id"]: w.get("monthly_savings_pct") for w in report["workloads"]}
    result = realized_savings(before, _stats(args), catalog=load_skus(), predicted_pct=predicted)
    if args.json:
        print(result.model_dump_json(indent=2))
        return
    pct = lambda v: "—" if v is None else f"{v:+.1f}%" if v < 0 else f"{v:.1f}%"
    t = Table(title=f"Realized savings since run {run_dir.name} (reservation cost, $0.00, no LLM)", title_justify="left")
    for col in ("workload", "before", "now", "realized", "predicted", "gap"):
        t.add_column(col, justify="left" if col == "workload" else "right")
    for r in result.rows:
        t.add_row(r.workload, f"${r.monthly_before_usd:,.2f}", f"${r.monthly_after_usd:,.2f}", pct(r.realized_savings_pct),
                  pct(r.predicted_savings_pct), "—" if r.prediction_gap_pct is None else f"{r.prediction_gap_pct:+.1f} pts")
    console.print(t)
    console.print(result.summary)


def cmd_analyze(args: argparse.Namespace) -> None:
    from kubesage.report import print_analysis
    from kubesage.service import AnalysisError, analyze_now

    try:
        result, run_dir, models = analyze_now(vm=args.vm, namespace=args.namespace, days=args.days, tz=args.tz,
                                              mode=args.mode, out_dir=args.out_dir)
    except AnalysisError as exc:
        sys.exit(str(exc))
    print_analysis(console, result, models, str(run_dir))
    console.print(f"\nreport: {run_dir}/report.json")


def cmd_apply(args: argparse.Namespace) -> None:
    from kubesage import apply

    try:
        run_dir = Path(args.run) if args.run else apply.latest_run(args.out_dir)
    except FileNotFoundError as exc:
        sys.exit(str(exc))
    steps = apply.plan(run_dir, dry_run=args.dry_run,
                       available_node_counts=apply.ready_node_counts(load_skus().node_label))
    if not steps:
        sys.exit(f"{run_dir} has no manifests to apply")
    console.print(f"applying {run_dir}{' (server dry-run)' if args.dry_run else ''}")
    failed = 0
    for step in apply.run(steps):
        if step.skipped:
            console.print(f"  [yellow]skip[/] {step.file.name}: {step.skipped}")
            continue
        ok = step.returncode == 0
        failed += not ok
        console.print(f"  [{'green' if ok else 'red'}]{'ok ' if ok else 'ERR'}[/] {step.file.name}: {step.output}")
    if failed:
        sys.exit(f"{failed} step(s) failed")


def cmd_gitops(args: argparse.Namespace) -> None:
    from kubesage import apply, gitops

    try:
        run_dir = Path(args.run) if args.run else apply.latest_run(args.out_dir)
        out = gitops.export(run_dir, Path(args.repo), path=args.path, kustomize_base=args.kustomize_base,
                            branch=args.branch, base_branch=args.base_branch, remote=args.remote,
                            push=args.push, open_pr=args.open_pr)
    except (FileNotFoundError, gitops.GitOpsError) as exc:
        sys.exit(str(exc))
    console.print(f"committed {out.commit[:10]} on branch {out.branch} in {args.repo} ({len(out.files)} files under {args.path}/)")
    if out.pr_url:
        console.print(f"pull request: {out.pr_url}")
    elif out.pushed:
        console.print(f"pushed {out.branch} to {args.remote}")
    else:
        console.print("not pushed; re-run with --push or --open-pr when ready")


def cmd_serve(args: argparse.Namespace) -> None:
    import faulthandler
    import signal

    from kubesage.web.server import Settings, serve

    faulthandler.register(signal.SIGUSR1)  # `kill -USR1 <pid>` prints every thread's stack, for a stuck run

    settings = Settings(vm=args.vm, namespace=args.namespace, days=args.days, tz=args.tz, mode=args.mode,
                        out_dir=Path(args.out_dir), gitops_repo=Path(args.gitops_repo) if args.gitops_repo else None,
                        gitops_base=args.gitops_base, gitops_path=args.gitops_path, alertmanager=args.alertmanager,
                        agent_prs=args.agent_prs, auto_agents=not args.no_auto_agents)
    server = serve(settings, args.host, args.port, background=True)
    console.print(f"KubeSage AI Control Center on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        if os.environ.get("KUBESAGE_API_TOKEN"):
            console.print("[yellow]note[/] non-loopback bind: mutating POSTs require the KUBESAGE_API_TOKEN")
        else:
            console.print("[red]warning[/] anyone who can reach this address can apply changes to the cluster; "
                          "set KUBESAGE_API_TOKEN to require a token for changes")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_spike(args: argparse.Namespace) -> None:
    from kubesage import live

    job = live.spike_job(namespace=args.namespace, target=args.target, rps=args.rps, work_ms=args.work_ms,
                         seconds=args.seconds)
    proc = live.start_spike(job)
    if proc.returncode != 0:
        sys.exit(f"could not start spike: {proc.stderr.strip()}")
    cores = args.rps * args.work_ms / 1000
    name = job["metadata"]["name"]
    console.print(f"started {name}: {args.rps:g} rps x {args.work_ms} ms = ~{cores:.1f} cores on {args.target} "
                  f"for {args.seconds}s")
    console.print(f"  watch: kubectl -n {args.namespace} get hpa,pods -w    "
                  f"latency: kubectl -n {args.namespace} logs -f job/{name}")


ANSI = None


def _clean(text: str) -> str:
    """Terminal bytes from a tmux pane as plain text: no escape sequences, carriage returns resolved."""
    import re
    global ANSI
    ANSI = ANSI or re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[()][0-9A-Za-z]|[=>78DEHMc])")
    text = ANSI.sub("", text.rstrip("\r\n"))
    return "".join(ch for ch in text.rsplit("\r", 1)[-1] if ch == "\t" or ch >= " ")


def cmd_ring(args: argparse.Namespace) -> None:
    from kubesage import ring

    streams = [s for s in (args.streams or "").split(",") if s] or None
    if args.action == "init":
        r = ring.Ring(size_mb=args.mb)
        console.print(f"ring at {r.path}: {r.stat()['capacity_bytes'] / 2 ** 20:.0f} MiB")
        return
    if args.action == "write":  # tmux pipe-pane target: one record per line of the pane's output
        r = ring.Ring()
        for raw in sys.stdin.buffer:
            line = _clean(raw.decode("utf-8", "replace").rstrip("\n"))
            if line.strip():
                r.write(args.stream, line)
        return
    r = ring.Ring(create=False)
    if args.action == "stat":
        st = r.stat()
        console.print(f"{st['path']}: {st['used_bytes'] / 2 ** 20:.1f} of {st['capacity_bytes'] / 2 ** 20:.0f} MiB, "
                      f"{st['records']:,} records{', wrapped' if st['wrapped'] else ''}")
        return
    if args.action == "dump":
        since = time.time() - args.minutes * 60 if args.minutes else 0
        for rec in r.iter_since(since, streams=streams):
            print(ring.format_line(rec), flush=False)
        return
    recs, last = r.tail(args.lines, streams=streams)  # tail
    show = not streams or len(streams) > 1 or any(s.endswith("*") for s in streams)
    for rec in recs:
        print(ring.format_line(rec, show_stream=show), flush=True)
    while args.follow:
        time.sleep(0.3)
        recs, last = r.read(last, streams=streams)
        for rec in recs:
            print(ring.format_line(rec, show_stream=show), flush=True)


def cmd_tmux(args: argparse.Namespace) -> None:
    """A tmux session that shows the ring: one pane per agent, test cases, journal, and an operator shell whose
    output is piped into the ring (so the web tmux view mirrors it, read-only)."""
    import shlex
    import shutil
    import subprocess

    from kubesage.agents.loader import load_team

    tmux = shutil.which("tmux") or str(Path(__file__).resolve().parent.parent / ".bin" / "tmux")
    if not Path(tmux).exists():
        sys.exit("tmux not found: run scripts/install-tools.sh (it fetches one into .bin without sudo)")
    exe = f"{shlex.quote(sys.executable)} -m kubesage.cli"  # the same interpreter and code as this command
    tail = lambda streams: f"{exe} ring tail -f -n 200 --streams {streams}"
    t = lambda *a: subprocess.run([tmux, *a], check=True)
    if subprocess.run([tmux, "has-session", "-t", args.session], capture_output=True).returncode != 0:
        agents = [a.name for a in load_team().agents]
        t("new-session", "-d", "-s", args.session, "-n", "agents", "-x", "240", "-y", "60", tail(f"agent/{agents[0]}"))
        for name in agents[1:]:
            t("split-window", "-t", f"{args.session}:agents", tail(f"agent/{name}"))
            t("select-layout", "-t", f"{args.session}:agents", "tiled")
        t("new-window", "-t", args.session, "-n", "testcases", tail("testcase/*,alerts,agent/team"))
        t("split-window", "-t", f"{args.session}:testcases", "-h", tail("watch/pods,watch/events"))
        t("new-window", "-t", args.session, "-n", "journal", tail("journal,access"))
        t("new-window", "-t", args.session, "-n", "ops")
        t("pipe-pane", "-t", f"{args.session}:ops", "-O", f"{exe} ring write --stream tmux/ops")
        for i, title in enumerate(agents):
            t("select-pane", "-t", f"{args.session}:agents.{i}", "-T", title)
        t("set-option", "-t", args.session, "pane-border-status", "top")
        t("select-window", "-t", f"{args.session}:agents")
        console.print(f"tmux session {args.session!r}: windows agents, testcases, journal, ops "
                      f"(ops is piped into the ring)")
    if args.attach:
        os.execvp(tmux, [tmux, "attach", "-t", args.session])


def cmd_agents(args: argparse.Namespace) -> None:
    from kubesage.agents import team as T
    from kubesage.agents.loader import load_team

    t = load_team()
    if args.action == "list":
        console.print(f"profile [cyan]{t.profile}[/] ({t.provider.base_url}); rules: {', '.join(n for n, _ in t.rules) or 'none'}")
        for a in t.agents:
            console.print(f"  [cyan]{a.name:<10}[/] {a.config.role:<9} {a.config.model:<7} {a.tier.label or ''} "
                          f"({a.tier.model})  {a.config.description}")
        return
    s = T.TeamSettings(vm=args.vm, alertmanager=args.alertmanager, out_dir=Path(args.out_dir) / "agents", mode=args.mode,
                       open_pr=args.open_pr)
    tr = T.run(t, s, trigger="manual")
    d = tr.snapshot()
    console.print(f"team run {d['id']}: {d['state']} ({d['mode']}), ${d['cost_usd']:.4f}" + (f" — {d['error']}" if d["error"] else ""))
    for issue in (d.get("diagnosis") or {}).get("issues", []):
        console.print(f"  [yellow]{issue['id']}[/] {issue['title']}: {issue['root_cause']}")
    ex = d.get("executor") or {}
    if ex.get("commit"):
        console.print(f"  committed {ex['commit'][:8]} on {ex['branch']} in {s.out_dir / 'repo'}")
    if args.open_pr and ex.get("commit"):
        console.print(f"  PR: {T.open_pr(tr, s, s.out_dir / d['id'])}")


def cmd_testcase(args: argparse.Namespace) -> None:
    from kubesage import lab

    if args.action == "list":
        active = lab.status(lab.lab_pods())
        for tc in lab.CATALOG:
            on = active.get(tc.name)
            console.print(f"  [cyan]{tc.name:<13}[/] {tc.title:<32} {'[green]running[/] ' + str(on['phases']) if on else ''}")
            console.print(f"                {tc.summary}")
        return
    if args.name not in lab.BY_NAME:
        sys.exit(f"unknown test case {args.name!r}; one of {', '.join(lab.BY_NAME)}")
    steps = (lab.start if args.action == "start" else lab.stop)(lab.BY_NAME[args.name])
    for s in steps:
        console.print(f"{'[green]ok[/]' if s['ok'] else '[red]FAILED[/]'} {s['cmd']}\n{s['output']}")
    if not all(s["ok"] for s in steps):
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kubesage", description="AI FinOps agent for Kubernetes")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sim = sub.add_parser("simulate", help="generate N days of synthetic demo metrics and backfill them")
    _common(sim)
    sim.add_argument("--step", type=int, default=60, help="scrape interval in seconds")
    sim.add_argument("--seed", type=int, default=42)
    sim.add_argument("--out", help="write VictoriaMetrics JSON lines to this file instead of importing")
    sim.add_argument("--keep-existing", action="store_true", help="do not delete the namespace's old series first")
    sim.add_argument("--no-fleet", dest="fleet", action="store_false",
                     help="only the demo namespace: skip the other namespaces, nodes and volumes `kubesage costs` reads")
    sim.set_defaults(func=cmd_simulate)

    co = sub.add_parser("costs", help="cost allocation for the whole cluster: by namespace, workload or team, "
                                      "with idle cost, budgets and waste findings (no LLM)")
    _cluster_wide(co)
    co.add_argument("--by", choices=("namespace", "workload", "team"), default="namespace")
    co.add_argument("--top", type=int, default=25, help="rows to print")
    co.add_argument("--json", action="store_true", help="the whole report as JSON")
    co.add_argument("--prometheus", action="store_true", help="Prometheus exposition text, as served on /metrics")
    co.set_defaults(func=cmd_costs)

    wi = sub.add_parser("whatif", help="what a bigger rush would need and cost: replicas and dollars for a "
                                       "traffic multiplier, per calendar event or recurring peak (no LLM)")
    _common(wi)
    wi.add_argument("--multiplier", type=float, required=True, help="traffic against normal load, e.g. 5 for five times")
    wi.add_argument("--event", help="only this event from config/calendar.yaml")
    wi.add_argument("--json", action="store_true")
    wi.set_defaults(func=cmd_whatif)

    ca = sub.add_parser("calendar", help="draft business-calendar events from the metrics (suggest) or an "
                                         ".ics export (import); writes only with --approve")
    _common(ca)
    ca.add_argument("action", choices=("suggest", "import"))
    ca.add_argument("file", nargs="?", help="import: the .ics file")
    ca.add_argument("--multiplier", type=float, default=1.5,
                    help="import: placeholder traffic multiplier for every event (an .ics knows when, not how much)")
    ca.add_argument("--approve", nargs="+", metavar="NAME",
                    help="append these drafted events to config/calendar.yaml ('all' for every one)")
    ca.set_defaults(func=cmd_calendar)

    rl = sub.add_parser("realized", help="did the promised saving show up? compares an analysis run's snapshot "
                                         "with the cluster now (no LLM)")
    _common(rl)
    rl.add_argument("run", nargs="?", help="run directory (default: the latest under --out-dir)")
    rl.add_argument("--out-dir", default="out")
    rl.add_argument("--json", action="store_true")
    rl.set_defaults(func=cmd_realized)

    bu = sub.add_parser("budgets", help="check config/costs.yaml budgets; exits 1 when one is over")
    _cluster_wide(bu)
    bu.add_argument("--json", action="store_true")
    bu.set_defaults(func=cmd_budgets)

    st = sub.add_parser("stats", help="Stage 1 only: deterministic stats, no LLM calls")
    _common(st)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    an = sub.add_parser("analyze", help="full pipeline: stats -> triage -> recommendations -> node pool")
    _common(an)
    an.add_argument("--mode", choices=("auto", "mock", "live"), default="auto",
                    help="auto = live if the API key env var is set, else mock")
    an.add_argument("--out-dir", default="out")
    an.set_defaults(func=cmd_analyze)

    ap = sub.add_parser("apply", help="apply a run's manifests to the cluster (latest run by default)")
    ap.add_argument("run", nargs="?", help="run directory, e.g. out/20261027-153916")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--dry-run", action="store_true", help="server-side dry run, change nothing")
    ap.set_defaults(func=cmd_apply)

    go = sub.add_parser("gitops", help="commit a run to a GitOps repo as a kustomize overlay, optionally open a PR")
    go.add_argument("--repo", required=True, help="path to a local clone of the GitOps repo")
    go.add_argument("--kustomize-base", required=True,
                    help="kustomize resource the overlay patches, relative to --path (e.g. ../base)")
    go.add_argument("--path", default="kubesage/demo", help="overlay directory inside the repo")
    go.add_argument("--run", help="run directory (default: latest under --out-dir)")
    go.add_argument("--out-dir", default="out")
    go.add_argument("--branch", help="default: kubesage/rightsize-<run>")
    go.add_argument("--base-branch", help="default: the repo's current branch")
    go.add_argument("--remote", default="origin")
    go.add_argument("--push", action="store_true", help="push the branch")
    go.add_argument("--open-pr", action="store_true", help="push and open a PR with gh")
    go.set_defaults(func=cmd_gitops)

    sv = sub.add_parser("serve", help="web UI: analyze, apply, spike and watch the cluster live")
    _common(sv)
    sv.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to present from another machine (e.g. over Tailscale)")
    sv.add_argument("--port", type=int, default=8600)
    sv.add_argument("--mode", choices=("auto", "mock", "live"), default="auto")
    sv.add_argument("--out-dir", default="out")
    sv.add_argument("--gitops-repo", help="enable the GitOps PR button for this local repo clone")
    sv.add_argument("--gitops-base", help="kustomize base relative to --gitops-path, e.g. ../base")
    sv.add_argument("--gitops-path", default="kubesage/demo")
    sv.add_argument("--alertmanager", default=os.environ.get("KUBESAGE_ALERTMANAGER_URL", "http://localhost:9193"))
    sv.add_argument("--agent-prs", action="store_true",
                    help="let the Executor agent push branches and open pull requests on this repo's origin")
    sv.add_argument("--no-auto-agents", action="store_true", help="don't run the agent team when a lab alert fires")
    sv.set_defaults(func=cmd_serve)

    sp = sub.add_parser("spike", help="live demo: send a burst of CPU-heavy traffic to a demo Service")
    sp.add_argument("--namespace", default="demo")
    sp.add_argument("--target", default="checkout-web", help="Service name (port 8080)")
    sp.add_argument("--rps", type=float, default=40)
    sp.add_argument("--work-ms", type=int, default=50, help="CPU milliseconds burned per request")
    sp.add_argument("--seconds", type=int, default=180)
    sp.set_defaults(func=cmd_spike)

    rg = sub.add_parser("ring", help="the RAM ring buffer every terminal stream goes through (tmux view, web)")
    rg.add_argument("action", choices=("tail", "dump", "stat", "write", "init"))
    rg.add_argument("--streams", help="comma-separated stream names; a trailing * matches a prefix, e.g. agent/*")
    rg.add_argument("-n", "--lines", type=int, default=100)
    rg.add_argument("-f", "--follow", action="store_true")
    rg.add_argument("--minutes", type=int, default=0, help="dump: only the last N minutes (0: everything held)")
    rg.add_argument("--stream", default="tmux/pane", help="write: the stream to write stdin to")
    rg.add_argument("--mb", type=int, default=None, help="init: size in MiB (default KUBESAGE_RING_MB or 500)")
    rg.set_defaults(func=cmd_ring)

    tx = sub.add_parser("tmux", help="open a tmux session over the ring: agents, test cases, journal, piped ops shell")
    tx.add_argument("--session", default="kubesage")
    tx.add_argument("--no-attach", dest="attach", action="store_false")
    tx.set_defaults(func=cmd_tmux)

    ag = sub.add_parser("agents", help="the agent team: list it, or run it once")
    ag.add_argument("action", choices=("list", "run"))
    ag.add_argument("--vm", default=os.environ.get("KUBESAGE_VM_URL", "http://localhost:8428"))
    ag.add_argument("--alertmanager", default=os.environ.get("KUBESAGE_ALERTMANAGER_URL", "http://localhost:9193"))
    ag.add_argument("--mode", choices=("auto", "mock", "live"), default="auto")
    ag.add_argument("--out-dir", default="out")
    ag.add_argument("--open-pr", action="store_true", help="push the Executor's branch and open a PR")
    ag.set_defaults(func=cmd_agents)

    tc = sub.add_parser("testcase", help="live test cases in the lab namespace")
    tc.add_argument("action", choices=("list", "start", "stop"))
    tc.add_argument("name", nargs="?")
    tc.set_defaults(func=cmd_testcase)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
