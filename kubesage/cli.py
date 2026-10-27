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
    for w in result.workloads:
        for name, text in w.manifests.items():
            (out_dir / "manifests" / name).write_text(text)
    (out_dir / "report.json").write_text(result.model_dump_json(indent=2))
    print_analysis(console, result, models, str(out_dir))
    console.print(f"\nreport: {out_dir}/report.json")


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

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
