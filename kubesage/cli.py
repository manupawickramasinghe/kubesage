"""kubesage simulate | stats | analyze"""
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
    p.add_argument("--namespace", default="demo")
    p.add_argument("--tz", default=os.environ.get("KUBESAGE_TZ", "Asia/Colombo"), help="timezone for daily patterns")
    p.add_argument("--days", type=int, default=30)


def cmd_simulate(args: argparse.Namespace) -> None:
    from kubesage.simulate.backfill import generate, import_to_vm, to_jsonl
    from kubesage.simulate.scenarios import DEMO_WORKLOADS

    end = int(time.time()) // 60 * 60
    started = time.perf_counter()
    series = generate(DEMO_WORKLOADS, namespace=args.namespace, days=args.days, end=end, step=args.step,
                      tz=args.tz, seed=args.seed)
    if args.out:
        Path(args.out).write_text("\n".join(to_jsonl(series)) + "\n")
        console.print(f"wrote {len(series)} series to {args.out}")
        return
    samples = import_to_vm(args.vm, series, namespace=args.namespace, replace=not args.keep_existing)
    console.print(f"backfilled {samples:,} samples ({len(series)} series, {args.days} days) into {args.vm} "
                  f"in {time.perf_counter() - started:.1f}s")
    for w in DEMO_WORKLOADS:
        console.print(f"  [cyan]{w.name:<16}[/] {w.story}")


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
    from kubesage.web.server import Settings, serve

    settings = Settings(vm=args.vm, namespace=args.namespace, days=args.days, tz=args.tz, mode=args.mode,
                        out_dir=Path(args.out_dir), gitops_repo=Path(args.gitops_repo) if args.gitops_repo else None,
                        gitops_base=args.gitops_base, gitops_path=args.gitops_path)
    server = serve(settings, args.host, args.port)
    console.print(f"KubeSage AI Control Center on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        console.print("[yellow]warning[/] anyone who can reach this address can apply changes to the cluster")
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kubesage", description="AI FinOps agent for Kubernetes")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sim = sub.add_parser("simulate", help="generate N days of synthetic demo metrics and backfill them")
    _common(sim)
    sim.add_argument("--step", type=int, default=60, help="scrape interval in seconds")
    sim.add_argument("--seed", type=int, default=42)
    sim.add_argument("--out", help="write VictoriaMetrics JSON lines to this file instead of importing")
    sim.add_argument("--keep-existing", action="store_true", help="do not delete the namespace's old series first")
    sim.set_defaults(func=cmd_simulate)

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
    sv.set_defaults(func=cmd_serve)

    sp = sub.add_parser("spike", help="live demo: send a burst of CPU-heavy traffic to a demo Service")
    sp.add_argument("--namespace", default="demo")
    sp.add_argument("--target", default="checkout-web", help="Service name (port 8080)")
    sp.add_argument("--rps", type=float, default=40)
    sp.add_argument("--work-ms", type=int, default=50, help="CPU milliseconds burned per request")
    sp.add_argument("--seconds", type=int, default=180)
    sp.set_defaults(func=cmd_spike)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
