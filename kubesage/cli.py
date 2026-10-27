"""kubesage simulate | stats | analyze"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console

from kubesage.config import load_models, load_skus

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


def _collect(args: argparse.Namespace):
    from kubesage.metrics.client import PromClient, PromError
    from kubesage.metrics.collect import collect

    try:
        stats, raw_points = collect(PromClient(args.vm), namespace=args.namespace, days=args.days,
                                    end=time.time(), tz=args.tz)
    except PromError as exc:
        sys.exit(f"metrics query failed: {exc}")
    if not stats:
        sys.exit(f"no workloads found in namespace {args.namespace!r} at {args.vm}; run `kubesage simulate` first")
    return stats, raw_points


def cmd_stats(args: argparse.Namespace) -> None:
    from kubesage.report import print_stats

    stats, _ = _collect(args)
    if args.json:
        print("[" + ",".join(s.model_dump_json() for s in stats) + "]")
    else:
        print_stats(console, stats)


def cmd_analyze(args: argparse.Namespace) -> None:
    from kubesage.llm.client import LLMClient, LLMError
    from kubesage.pipeline import analyze
    from kubesage.report import print_analysis

    models = load_models()
    mode = args.mode
    if mode == "auto":
        mode = "live" if os.environ.get(models.provider.api_key_env) else "mock"
    stats, raw_points = _collect(args)
    try:
        llm = LLMClient(models, mode)
        result = analyze(stats, llm=llm, catalog=load_skus(), namespace=args.namespace, days=args.days,
                         tz=args.tz, raw_points=raw_points)
    except LLMError as exc:
        sys.exit(f"LLM call failed: {exc}")

    out_dir = Path(args.out_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / "manifests").mkdir(parents=True, exist_ok=True)
    files = {name: text for w in result.workloads for name, text in w.manifests.items()}
    if result.node_pool:
        files.update(result.node_pool.migrations)
    for name, text in files.items():
        (out_dir / "manifests" / name).write_text(text)
    (out_dir / "report.json").write_text(result.model_dump_json(indent=2))
    print_analysis(console, result, models, str(out_dir))
    console.print(f"\nreport: {out_dir}/report.json")


def cmd_apply(args: argparse.Namespace) -> None:
    from kubesage import apply

    try:
        run_dir = Path(args.run) if args.run else apply.latest_run(args.out_dir)
    except FileNotFoundError as exc:
        sys.exit(str(exc))
    steps = apply.plan(run_dir, dry_run=args.dry_run, available_skus=apply.node_skus(load_skus().node_label))
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
