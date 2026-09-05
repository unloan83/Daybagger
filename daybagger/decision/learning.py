from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import sqrt
from pathlib import Path
from statistics import stdev


@dataclass(frozen=True, slots=True)
class LearnedModelStats:
    model_id: str
    observations: int
    avg_net_return_bps: float
    conservative_net_return_bps: float
    brier_score: float
    evidence_weight: float
    eligible: bool


class ModelLearningStore:
    """
    Evidence-based specialist reliability with stability protection.

    Production influence requires a minimum recent sample and positive lower
    confidence bound on realised net expectancy. One lucky observation can never
    create a positive model weight.
    """

    def __init__(
        self,
        path: Path,
        *,
        minimum_observations: int = 20,
        lookback_days: int = 90,
    ):
        if minimum_observations < 2:
            raise ValueError("minimum_observations must be >= 2")
        if lookback_days <= 0:
            raise ValueError("lookback_days must be > 0")
        self.path = path
        self.minimum_observations = minimum_observations
        self.lookback_days = lookback_days

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    probability REAL NOT NULL,
                    predicted_return_bps REAL NOT NULL,
                    realised_net_return_bps REAL NOT NULL,
                    favourable_outcome INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def record(
        self,
        *,
        model_id: str,
        probability: float,
        predicted_return_bps: float,
        realised_net_return_bps: float,
        favourable_outcome: bool,
        observed_at: datetime | None = None,
    ) -> None:
        if not model_id.strip():
            raise ValueError("model_id is required")
        if not 0.0 <= probability <= 1.0:
            raise ValueError("probability must be between 0 and 1")
        timestamp = observed_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO model_outcomes(
                    created_at_utc, model_id, probability, predicted_return_bps,
                    realised_net_return_bps, favourable_outcome
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.astimezone(timezone.utc).isoformat(),
                    model_id,
                    probability,
                    predicted_return_bps,
                    realised_net_return_bps,
                    int(favourable_outcome),
                ),
            )
            conn.commit()

    def stats(self, *, as_of: datetime | None = None) -> dict[str, LearnedModelStats]:
        reference = as_of or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        cutoff = reference.astimezone(timezone.utc) - timedelta(days=self.lookback_days)

        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                """
                SELECT created_at_utc, model_id, probability,
                       realised_net_return_bps, favourable_outcome
                FROM model_outcomes
                ORDER BY id
                """
            ).fetchall()

        grouped: dict[str, list[tuple[float, float, int]]] = {}
        for created_raw, model_id, p, ret, favourable in rows:
            created = datetime.fromisoformat(str(created_raw))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created.astimezone(timezone.utc) < cutoff:
                continue
            grouped.setdefault(str(model_id), []).append(
                (float(p), float(ret), int(favourable))
            )

        result: dict[str, LearnedModelStats] = {}
        for model_id, samples in grouped.items():
            n = len(samples)
            returns = [ret for _, ret, _ in samples]
            avg_ret = sum(returns) / n
            brier = sum((p - favourable) ** 2 for p, _, favourable in samples) / n
            eligible = n >= self.minimum_observations
            conservative = 0.0
            if eligible:
                sample_sd = stdev(returns) if n > 1 else 0.0
                standard_error = sample_sd / sqrt(n)
                conservative = avg_ret - _student_t_critical_95(n - 1) * standard_error
            calibration = max(0.0, 1.0 - brier)
            weight = max(0.0, conservative) * calibration if eligible else 0.0

            result[model_id] = LearnedModelStats(
                model_id=model_id,
                observations=n,
                avg_net_return_bps=avg_ret,
                conservative_net_return_bps=conservative,
                brier_score=brier,
                evidence_weight=weight,
                eligible=eligible,
            )
        return result

    def weights(self, *, as_of: datetime | None = None) -> dict[str, float]:
        return {
            mid: stat.evidence_weight
            for mid, stat in self.stats(as_of=as_of).items()
        }

    def vetoed_model_ids(self, *, as_of: datetime | None = None) -> frozenset[str]:
        """Return models with sufficient evidence for a conservative veto.

        Learning can suppress a proven-bad model, but it cannot promote an
        unproven model or improve a prediction beyond its validated artifact.
        """
        return frozenset(
            model_id
            for model_id, stat in self.stats(as_of=as_of).items()
            if stat.eligible and stat.conservative_net_return_bps <= 0
        )


def _student_t_critical_95(degrees_of_freedom: int) -> float:
    """
    Two-sided 95% Student-t critical value without a SciPy runtime dependency.

    Runtime learning starts at n>=20, so a Cornish-Fisher expansion around the
    normal 0.975 quantile is accurate enough while remaining conservative versus
    the previous fixed 1.96 multiplier for small recent samples.
    """
    if degrees_of_freedom <= 0:
        raise ValueError("degrees_of_freedom must be positive")
    z = 1.959963984540054
    v = float(degrees_of_freedom)
    z2 = z * z
    z3 = z2 * z
    z5 = z3 * z2
    z7 = z5 * z2
    term1 = (z3 + z) / (4.0 * v)
    term2 = (5.0 * z5 + 16.0 * z3 + 3.0 * z) / (96.0 * v * v)
    term3 = (3.0 * z7 + 19.0 * z5 + 17.0 * z3 - 15.0 * z) / (384.0 * v**3)
    return z + term1 + term2 + term3
