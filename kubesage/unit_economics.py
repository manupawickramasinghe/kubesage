"""Unit economics: what a workload costs per unit of business it serves.

Dollars per month tell an operator the bill; dollars per 1,000 requests (or checkouts, or jobs) tell
them whether the service is getting more or less efficient as it grows. KubeSage measures CPU and
memory, not business traffic, so the traffic figure is an operator input — from an ingress, an APM
or a billing export. It is never inferred from CPU: a made-up request count would make the unit
cost made up too.
"""
from __future__ import annotations

from pydantic import BaseModel

DEFAULT_HOURS_PER_MONTH = 730.0  # matches config/skus.yaml hours_per_month


def requests_per_month(avg_rps: float, hours_per_month: float = DEFAULT_HOURS_PER_MONTH) -> float:
    """Monthly request volume for a steady average rate (730h month, the costing convention)."""
    if avg_rps < 0:
        raise ValueError("avg_rps cannot be negative")
    return avg_rps * hours_per_month * 3600


def cost_per_1k_requests(monthly_usd: float, monthly_requests: float) -> float | None:
    """USD per 1,000 requests. None when there is no traffic to divide by — never a fake number."""
    if monthly_requests <= 0:
        return None
    return round(monthly_usd / monthly_requests * 1000, 6)  # unit costs are small; 4dp would erase real differences


class UnitEconomics(BaseModel):
    workload: str
    unit: str  # what one unit of business is: "request", "checkout", "job", ...
    monthly_usd: float
    monthly_units: float
    cost_per_1k_usd: float | None
    monthly_after_usd: float | None = None
    cost_per_1k_after_usd: float | None = None
    unit_savings_pct: float | None = None  # positive: each 1k units got cheaper

    @property
    def summary(self) -> str:
        if self.cost_per_1k_usd is None:
            return f"{self.workload}: ${self.monthly_usd:,.2f}/mo, no traffic figure to price per {self.unit}"
        text = f"{self.workload}: ${self.cost_per_1k_usd:.4f} per 1k {self.unit}s (${self.monthly_usd:,.2f}/mo)"
        if self.cost_per_1k_after_usd is not None and self.unit_savings_pct is not None:
            direction = "cheaper" if self.unit_savings_pct >= 0 else "more expensive"
            text += (f" -> ${self.cost_per_1k_after_usd:.4f} after the plan "
                     f"({abs(self.unit_savings_pct):.1f}% {direction} per 1k {self.unit}s)")
        return text


def unit_economics(workload: str, monthly_usd: float, *, avg_rps: float | None = None,
                   monthly_units: float | None = None, monthly_after_usd: float | None = None,
                   unit: str = "request",
                   hours_per_month: float = DEFAULT_HOURS_PER_MONTH) -> UnitEconomics:
    """Price one workload per 1k units of business. Supply either a steady average RPS or a measured
    monthly unit count (the measured count wins: real traffic is bursty, RPS x seconds is an estimate)."""
    if monthly_units is None:
        monthly_units = requests_per_month(avg_rps, hours_per_month) if avg_rps is not None else 0.0
    before = cost_per_1k_requests(monthly_usd, monthly_units)
    after = cost_per_1k_requests(monthly_after_usd, monthly_units) if monthly_after_usd is not None else None
    unit_pct = None
    if before is not None and after is not None and before > 0:
        unit_pct = round((before - after) / before * 100, 1)
    return UnitEconomics(workload=workload, unit=unit, monthly_usd=round(monthly_usd, 2),
                         monthly_units=round(monthly_units, 1), cost_per_1k_usd=before,
                         monthly_after_usd=monthly_after_usd, cost_per_1k_after_usd=after,
                         unit_savings_pct=unit_pct)
