from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Sequence

from daybagger.data.upstox import IntradayCandle
from daybagger.decision.learning import ModelLearningStore
from daybagger.domain import Direction, ModelOpinion


@dataclass(frozen=True, slots=True)
class ObservedOutcome:
    model_id: str
    realised_net_return_bps: float
    favourable: bool


class OutcomeLearner:
    """Label executed and rejected opinions from genuine future minute candles."""

    def __init__(self, learning_store: ModelLearningStore):
        self.learning_store = learning_store

    def record_horizon_outcomes(
        self,
        *,
        opinions: Sequence[ModelOpinion],
        entry_reference_price: Decimal,
        future_candles: Sequence[IntradayCandle],
        estimated_cost_bps: float,
    ) -> list[ObservedOutcome]:
        """Backward-compatible bar-count helper used by existing tests."""
        if entry_reference_price <= 0:
            raise ValueError("entry_reference_price must be positive")
        if not future_candles:
            raise ValueError("future_candles are required")
        ordered = sorted(future_candles, key=lambda c: c.timestamp)
        outcomes: list[ObservedOutcome] = []
        for op in opinions:
            horizon_bars = min(op.horizon_minutes, len(ordered))
            if horizon_bars <= 0:
                continue
            outcomes.append(
                self._record(
                    op=op,
                    entry_price=entry_reference_price,
                    exit_price=ordered[horizon_bars - 1].close,
                    estimated_cost_bps=estimated_cost_bps,
                    observed_at=ordered[horizon_bars - 1].timestamp,
                )
            )
        return outcomes

    def record_aligned_outcomes(
        self,
        *,
        opinions: Sequence[ModelOpinion],
        decision_at,
        session_candles: Sequence[IntradayCandle],
        estimated_cost_bps: float,
    ) -> list[ObservedOutcome]:
        """
        Same timing convention as historical validation: decision at bar T,
        entry at the next genuine one-minute bar OPEN, outcome at T+h CLOSE.

        Missing exact timestamps are skipped rather than interpolated/fabricated.
        """
        if decision_at.tzinfo is None:
            raise ValueError("decision_at must be timezone-aware")
        ordered = sorted(session_candles, key=lambda c: c.timestamp)
        next_bars = [bar for bar in ordered if bar.timestamp > decision_at]
        if not next_bars:
            return []
        entry = next_bars[0]
        by_ts = {bar.timestamp: bar for bar in ordered}
        outcomes: list[ObservedOutcome] = []
        for op in opinions:
            target = op.as_of + timedelta(minutes=op.horizon_minutes)
            exit_bar = by_ts.get(target)
            if exit_bar is None or target <= entry.timestamp:
                continue
            outcomes.append(
                self._record(
                    op=op,
                    entry_price=entry.open,
                    exit_price=exit_bar.close,
                    estimated_cost_bps=estimated_cost_bps,
                    observed_at=exit_bar.timestamp,
                )
            )
        return outcomes

    def _record(
        self,
        *,
        op: ModelOpinion,
        entry_price: Decimal,
        exit_price: Decimal,
        estimated_cost_bps: float,
        observed_at,
    ) -> ObservedOutcome:
        if entry_price <= 0 or exit_price <= 0:
            raise ValueError("outcome prices must be positive")
        raw_bps = float((exit_price / entry_price - Decimal("1")) * Decimal("10000"))
        if op.direction == Direction.SHORT:
            raw_bps = -raw_bps
        net_bps = raw_bps - estimated_cost_bps
        favourable = net_bps > 0
        self.learning_store.record(
            model_id=op.model_id,
            probability=op.probability,
            predicted_return_bps=op.expected_return_bps,
            realised_net_return_bps=net_bps,
            favourable_outcome=favourable,
            observed_at=observed_at,
        )
        return ObservedOutcome(op.model_id, net_bps, favourable)
