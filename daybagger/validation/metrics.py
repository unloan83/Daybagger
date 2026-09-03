from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean
from typing import Sequence


@dataclass(frozen=True, slots=True)
class PredictionOutcome:
    predicted_probability: float
    realised_net_return_bps: float


@dataclass(frozen=True, slots=True)
class ValidationMetrics:
    observations: int
    avg_net_return_bps: float
    median_net_return_bps: float
    win_rate: float
    profit_factor: float | None
    max_drawdown_bps: float
    brier_score: float
    return_stability: float


def evaluate_predictions(
    outcomes: Sequence[PredictionOutcome],
) -> ValidationMetrics:
    if not outcomes:
        raise ValueError("at least one outcome is required")

    returns = [float(o.realised_net_return_bps) for o in outcomes]
    probs = [float(o.predicted_probability) for o in outcomes]

    for p in probs:
        if not 0.0 <= p <= 1.0:
            raise ValueError("predicted_probability must be between 0 and 1")

    ordered = sorted(returns)
    n = len(ordered)
    if n % 2:
        med = ordered[n // 2]
    else:
        med = (ordered[n // 2 - 1] + ordered[n // 2]) / 2

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else None

    favourable = [1.0 if r > 0 else 0.0 for r in returns]
    brier = mean((p - y) ** 2 for p, y in zip(probs, favourable))

    curve = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        curve += r
        peak = max(peak, curve)
        max_dd = max(max_dd, peak - curve)

    avg = mean(returns)
    if n > 1:
        variance = sum((r - avg) ** 2 for r in returns) / (n - 1)
        stdev = sqrt(variance)
        stability = avg / stdev if stdev > 0 else (1.0 if avg > 0 else 0.0)
    else:
        stability = 0.0

    return ValidationMetrics(
        observations=n,
        avg_net_return_bps=avg,
        median_net_return_bps=med,
        win_rate=len(wins) / n,
        profit_factor=pf,
        max_drawdown_bps=max_dd,
        brier_score=brier,
        return_stability=stability,
    )
