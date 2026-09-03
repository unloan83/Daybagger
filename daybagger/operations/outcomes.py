from __future__ import annotations

from dataclasses import dataclass
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
    """
    Labels model opinions from observed future candles.

    This works for executed AND rejected opportunities, so Daybagger can learn
    whether a filter/model missed useful moves.
    """

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
            exit_price = ordered[horizon_bars - 1].close
            raw_bps = float(
                (exit_price / entry_reference_price - Decimal("1"))
                * Decimal("10000")
            )
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
            )
            outcomes.append(
                ObservedOutcome(
                    model_id=op.model_id,
                    realised_net_return_bps=net_bps,
                    favourable=favourable,
                )
            )
        return outcomes
