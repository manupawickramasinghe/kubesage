"""Realized savings: did the saving the report promised actually show up after Apply?

Every percentage KubeSage prints at analysis time is a model of the future. This module closes the loop
with the same allocation costing applied to two real Stage 1 snapshots — one from before the change, one
from after — and compares the measured percent against the predicted one. "Predicted saves 12.0%,
realized 10.4%" is a claim an operator can audit; the prediction alone is marketing.
"""
from __future__ import annotations

from pydantic import BaseModel

from kubesage.config import SkuCatalog
from kubesage.metrics.stats import WorkloadStats
from kubesage.recommend import costing


class RealizedRow(BaseModel):
    workload: str
    monthly_before_usd: float
    monthly_after_usd: float
    realized_savings_usd: float
    realized_savings_pct: float | None
    predicted_savings_pct: float | None = None
    prediction_gap_pct: float | None = None  # realized - predicted, in percentage points


class RealizedReport(BaseModel):
    rows: list[RealizedRow]
    monthly_before_usd: float
    monthly_after_usd: float
    realized_savings_usd: float
    realized_savings_pct: float | None
    summary: str


def _allocation(s: WorkloadStats, catalog: SkuCatalog) -> float:
    return costing.allocation_monthly(s.cpu_request_cores or 0.0, s.mem_request_mib or 0.0,
                                      s.replicas, catalog)


def realized_savings(before: list[WorkloadStats], after: list[WorkloadStats], *,
                     catalog: SkuCatalog,
                     predicted_pct: dict[str, float | None] | None = None) -> RealizedReport:
    """Compare two snapshots workload by workload; workloads missing on either side are skipped."""
    after_by_id = {s.id: s for s in after}
    predicted_pct = predicted_pct or {}
    rows = []
    for b in before:
        a = after_by_id.get(b.id)
        if a is None:
            continue
        before_usd, after_usd = _allocation(b, catalog), _allocation(a, catalog)
        pct = costing.savings_pct(before_usd, after_usd)
        predicted = predicted_pct.get(b.id)
        gap = round(pct - predicted, 1) if pct is not None and predicted is not None else None
        rows.append(RealizedRow(workload=b.id, monthly_before_usd=round(before_usd, 2),
                                monthly_after_usd=round(after_usd, 2),
                                realized_savings_usd=round(before_usd - after_usd, 2),
                                realized_savings_pct=pct, predicted_savings_pct=predicted,
                                prediction_gap_pct=gap))
    total_before = sum(r.monthly_before_usd for r in rows)
    total_after = sum(r.monthly_after_usd for r in rows)
    total_pct = costing.savings_pct(total_before, total_after)
    summary = (f"Realized saving across {len(rows)} workload(s): ${total_before:,.2f} -> "
               f"${total_after:,.2f}/mo"
               + (f" ({'saves' if (total_pct or 0) >= 0 else 'costs'} {abs(total_pct):.1f}%)"
                  if total_pct is not None else ""))
    return RealizedReport(rows=rows, monthly_before_usd=round(total_before, 2),
                          monthly_after_usd=round(total_after, 2),
                          realized_savings_usd=round(total_before - total_after, 2),
                          realized_savings_pct=total_pct, summary=summary)
