from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class LearnedModelStats:
    model_id: str
    observations: int
    avg_net_return_bps: float
    brier_score: float
    evidence_weight: float


class ModelLearningStore:
    """
    Evidence-based learning for specialist reliability.

    Stores actual model predictions and observed outcomes. Weights are derived from
    realised net expectancy and probability calibration; missing evidence means zero
    weight rather than fabricated performance.
    """

    def __init__(self, path: Path):
        self.path = path

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
    ) -> None:
        if not model_id.strip():
            raise ValueError("model_id is required")
        if not 0.0 <= probability <= 1.0:
            raise ValueError("probability must be between 0 and 1")

        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO model_outcomes(
                    created_at_utc, model_id, probability, predicted_return_bps,
                    realised_net_return_bps, favourable_outcome
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    model_id,
                    probability,
                    predicted_return_bps,
                    realised_net_return_bps,
                    int(favourable_outcome),
                ),
            )
            conn.commit()

    def stats(self) -> dict[str, LearnedModelStats]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                """
                SELECT model_id, probability, realised_net_return_bps, favourable_outcome
                FROM model_outcomes
                ORDER BY id
                """
            ).fetchall()

        grouped: dict[str, list[tuple[float, float, int]]] = {}
        for model_id, p, ret, favourable in rows:
            grouped.setdefault(model_id, []).append((float(p), float(ret), int(favourable)))

        result: dict[str, LearnedModelStats] = {}
        for model_id, samples in grouped.items():
            n = len(samples)
            avg_ret = sum(ret for _, ret, _ in samples) / n
            brier = sum((p - favourable) ** 2 for p, _, favourable in samples) / n
            calibration = max(0.0, 1.0 - brier)
            weight = max(0.0, avg_ret) * calibration

            result[model_id] = LearnedModelStats(
                model_id=model_id,
                observations=n,
                avg_net_return_bps=avg_ret,
                brier_score=brier,
                evidence_weight=weight,
            )
        return result

    def weights(self) -> dict[str, float]:
        return {mid: stat.evidence_weight for mid, stat in self.stats().items()}
