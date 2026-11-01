"""Forecast verification: after the fact, grade what the forecast predicted against what actually ran.

An agent that never checks its own predictions is just confident. Given a forecast made earlier and a
fresh Stage 1 record for the same workload, this reports the actual peak, the percentage error, an
accuracy score and a plain verdict (covered / under-forecast / over-forecast), so the UI can show
"predicted 2.76 cores at 85% confidence — actual 2.61, error 6%" instead of leaving the forecast
floating as a claim nobody audited.
"""
from __future__ import annotations

from pydantic import BaseModel

from kubesage.forecast import WorkloadForecast
from kubesage.metrics.stats import WorkloadStats


class Verification(BaseModel):
    workload: str
    expected_peak_cpu: float
    actual_peak_cpu: float
    error_pct: float | None      # signed: positive = actual came in above the forecast
    accuracy: float              # 0..1, 1 = perfect
    grade: str                   # A >= .90, B >= .75, C >= .50, else D
    verdict: str                 # covered | under-forecast | over-forecast
    confidence_was: float        # the confidence the forecast claimed, for calibration
    summary: str


def _grade(accuracy: float) -> str:
    return "A" if accuracy >= 0.90 else "B" if accuracy >= 0.75 else "C" if accuracy >= 0.50 else "D"


def verify(forecast: WorkloadForecast, actual: WorkloadStats) -> Verification:
    """Compare one forecast with the workload's later observed stats (same id)."""
    observed = max(actual.cpu_total_p95, max((p.peak_total_cpu for p in actual.peaks), default=0.0))
    expected = forecast.expected_peak_cpu
    if observed <= 0:
        return Verification(workload=forecast.workload, expected_peak_cpu=expected, actual_peak_cpu=0.0,
                            error_pct=None, accuracy=0.0, grade="D", verdict="under-forecast",
                            confidence_was=forecast.confidence,
                            summary=f"{actual.name}: no load observed to verify the forecast against.")
    error = (observed - expected) / observed * 100
    accuracy = round(max(0.0, 1 - abs(error) / 100), 2)
    verdict = "covered" if abs(error) <= 15 else ("under-forecast" if error > 0 else "over-forecast")
    grade = _grade(accuracy)
    return Verification(
        workload=forecast.workload, expected_peak_cpu=round(expected, 3), actual_peak_cpu=round(observed, 3),
        error_pct=round(error, 1), accuracy=accuracy, grade=grade, verdict=verdict,
        confidence_was=forecast.confidence,
        summary=(f"{actual.name}: forecast {expected:.2f} cores (confidence {forecast.confidence:.0%}), "
                 f"actual peak {observed:.2f} cores, error {error:+.1f}% — {verdict}, grade {grade}."))


def verify_all(forecasts: list[WorkloadForecast], actuals: list[WorkloadStats]) -> list[Verification]:
    by_id = {s.id: s for s in actuals}
    return [verify(f, by_id[f.workload]) for f in forecasts if f.workload in by_id]
