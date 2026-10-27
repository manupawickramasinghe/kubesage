"""Terminal rendering of stats and analysis results."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from kubesage.config import ModelsConfig
from kubesage.metrics.stats import WorkloadStats
from kubesage.pipeline import AnalysisResult

STATUS_STYLE = {"HEALTHY": "green", "ROUTINE": "yellow", "COMPLEX": "red"}


def _usd(v: float) -> str:
    return f"${v:,.4f}" if abs(v) < 1 else f"${v:,.2f}"


def _tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def stats_table(stats: list[WorkloadStats]) -> Table:
    t = Table(title="Stage 1 · deterministic stats ($0.00, no LLM)", title_justify="left")
    for col in ("workload", "pods", "cpu req", "cpu p95/pod", "mem req", "mem p99/pod", "restarts", "signals"):
        t.add_column(col, overflow="fold")
    for s in stats:
        t.add_row(s.name, str(s.replicas), f"{s.cpu_request_cores or 0:.2f}", f"{s.cpu_pod_p95:.3f}",
                  f"{s.mem_request_mib or 0:.0f}Mi", f"{s.mem_pod_p99_mib:.0f}Mi", str(s.restarts),
                  ", ".join(s.signals) or "-")
    return t


def print_stats(console: Console, stats: list[WorkloadStats]) -> None:
    console.print(stats_table(stats))
    for s in stats:
        for p in s.peaks:
            console.print(f"  [cyan]{s.name}[/] recurring peak days={p.days} {p.start_hour:02d}:00-{p.end_hour:02d}:00 "
                          f"~{p.peak_total_cpu:.2f} cores (baseline {p.baseline_total_cpu:.2f}, seen on {p.recurrence:.0%} of days)")


def print_analysis(console: Console, res: AnalysisResult, models: ModelsConfig, out_dir: str | None) -> None:
    label = {name: res.tiers.get(name, name) for name in models.tiers}
    mode = "[bold red]MOCK[/] (no API key: deterministic stand-ins, token counts estimated)" if res.mode == "mock" else "live"
    console.rule(f"KubeSage · {res.window_days}-day analysis of namespace [bold]{res.namespace}[/]")
    console.print(f"LLM mode: {mode}\n")
    print_stats(console, res.stats)

    tri = res.ledger[0]
    console.print(f"\n[bold]Stage 2 · triage[/] on {tri.model}: in {_tok(tri.input_tokens)} tok, "
                  f"out {_tok(tri.output_tokens)} tok, {_usd(tri.cost_usd)}")
    for w in res.workloads:
        route = f"-> {label[w.tier]} ({models.tiers[w.tier].model})" if w.tier else "-> stop (no more tokens)"
        console.print(f"  [{STATUS_STYLE[w.triage.status]}]{w.triage.status:<8}[/] {w.id:<26} {route}")

    console.print("\n[bold]Stage 3 · recommendations[/]")
    by_workload = {e.workload: e for e in res.ledger if e.stage == "recommend"}
    triage_share = tri.cost_usd / max(len(res.workloads), 1)
    for w in res.workloads:
        if not w.recommendation:
            continue
        rec, e = w.recommendation, by_workload[w.id]
        delta = w.monthly_before_usd - w.monthly_after_usd
        money = f"saves {_usd(delta)}/mo" if delta >= 0 else f"[red]costs {_usd(-delta)}/mo more[/]"
        body = [rec.diagnosis]
        body += [f"[magenta]guardrail[/] {n}" for n in w.guardrail_notes]
        body += [f"[blue]follow-up[/] {f}" for f in rec.follow_ups]
        if out_dir:
            body += [f"[dim]{out_dir}/manifests/{name}[/]" for name in w.manifests]
        trace = (f"{label['triage']} share {_usd(triage_share)} -> {label[w.tier]} {_tok(e.input_tokens)} in / "
                 f"{_tok(e.output_tokens)} out {_usd(e.cost_usd)} | diagnosis {_usd(triage_share + e.cost_usd)} | {money}")
        body.append(f"[dim]router trace: {trace}[/]")
        console.print(Panel("\n".join(body), title=f"{w.id} · risk {rec.risk} · "
                            f"{_usd(w.monthly_before_usd)} -> {_usd(w.monthly_after_usd)}/mo", title_align="left"))

    alloc_before = sum(w.monthly_before_usd for w in res.workloads)
    alloc_after = sum(w.monthly_after_usd for w in res.workloads)
    console.print(f"\n[bold]Requested capacity (allocation view):[/] {_usd(alloc_before)}/mo -> {_usd(alloc_after)}/mo")
    if res.node_pool:
        np_ = res.node_pool
        console.print(f"[bold]Node pool (billing view, {np_.choice.sku} chosen by "
                      f"{label['strategist']}):[/] {np_.before_nodes}x {np_.before_sku} {_usd(np_.before_monthly_usd)}/mo "
                      f"-> {np_.choice.nodes}x {np_.choice.sku} {_usd(np_.after_monthly_usd)}/mo "
                      f"([green]saves {_usd(np_.before_monthly_usd - np_.after_monthly_usd)}/mo[/])")
        console.print(f"  {np_.choice.rationale}")
        if np_.migrations:
            console.print(f"  [cyan]migration[/] {len(np_.migrations)} Deployments -> nodeSelector "
                          f"{np_.node_label}={np_.choice.sku} (*.migrate.yaml; skipped by apply if no such node)")
        for n in np_.notes:
            console.print(f"  [magenta]guardrail[/] {n}")

    t = Table(title="LLM spend for this run (FinOps for the FinOps bot)", title_justify="left")
    for col in ("stage", "workload", "model", "in", "cached", "out", "cost"):
        t.add_column(col)
    for e in res.ledger:
        t.add_row(e.stage, e.workload, e.model, _tok(e.input_tokens), _tok(e.cached_tokens), _tok(e.output_tokens),
                  _usd(e.cost_usd))
    console.print()
    console.print(t)
    b = res.baselines_usd
    console.print(f"Routed pipeline: [bold green]{_usd(res.llm_cost_usd)}[/] · same calls all on the strategist: "
                  f"{_usd(b['all_calls_on_strategist'])} · raw 5-min series to the strategist (est.): "
                  f"{_usd(b['raw_series_to_strategist'])}")
    for n in res.notes:
        console.print(f"[magenta]note[/] {n}")
