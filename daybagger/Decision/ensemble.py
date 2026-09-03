from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Mapping, Sequence

from daybagger.domain import DecisionStatus, Direction, ModelOpinion, Opportunity


class EnsembleError(RuntimeError):
    """Specialist opinions cannot be combined safely."""


class EvidenceWeightedEnsemble:
    """
    Combines specialist opinions using evidence-derived model weights.

    No arbitrary confidence threshold is embedded here. A direction is QUALIFIED
    only when its weighted expected return remains positive after supplied costs.
    """

    def rank(
        self,
        *,
        symbol: str,
        as_of: datetime,
        opinions: Sequence[ModelOpinion],
        model_weights: Mapping[str, float],
        estimated_round_trip_cost_bps: float,
    ) -> Opportunity:
        if as_of.tzinfo is None:
            raise EnsembleError("as_of must be timezone-aware")
        if estimated_round_trip_cost_bps < 0:
            raise EnsembleError("estimated_round_trip_cost_bps cannot be negative")

        usable = [
            op for op in opinions
            if op.symbol == symbol
            and op.direction in (Direction.LONG, Direction.SHORT)
            and model_weights.get(op.model_id, 0.0) > 0
        ]
        if not usable:
            return Opportunity.create(
                symbol=symbol,
                direction=Direction.FLAT,
                as_of=as_of,
                expected_net_return_bps=0.0,
                confidence=0.0,
                status=DecisionStatus.INSUFFICIENT_EVIDENCE,
                reason="NO_VALIDATED_WEIGHTED_OPINIONS",
                opinion_ids=[],
            )

        by_direction: dict[Direction, list[ModelOpinion]] = defaultdict(list)
        for op in usable:
            by_direction[op.direction].append(op)

        scored: list[tuple[Direction, float, float, list[ModelOpinion]]] = []
        for direction, group in by_direction.items():
            raw_weights = [float(model_weights[op.model_id]) for op in group]
            total = sum(raw_weights)
            if total <= 0:
                continue
            norm = [w / total for w in raw_weights]
            expected_gross = sum(
                w * op.expected_return_bps for w, op in zip(norm, group)
            )
            confidence = sum(
                w * op.probability for w, op in zip(norm, group)
            )
            net = expected_gross - estimated_round_trip_cost_bps
            scored.append((direction, net, confidence, group))

        if not scored:
            return Opportunity.create(
                symbol=symbol,
                direction=Direction.FLAT,
                as_of=as_of,
                expected_net_return_bps=0.0,
                confidence=0.0,
                status=DecisionStatus.INSUFFICIENT_EVIDENCE,
                reason="NO_DIRECTION_WITH_POSITIVE_MODEL_WEIGHT",
                opinion_ids=[],
            )

        scored.sort(key=lambda row: row[1], reverse=True)
        direction, net, confidence, group = scored[0]

        status = (
            DecisionStatus.QUALIFIED
            if net > 0
            else DecisionStatus.REJECTED
        )
        reason = (
            "POSITIVE_EXPECTED_NET_EDGE"
            if net > 0
            else "EXPECTED_EDGE_DOES_NOT_COVER_COSTS"
        )

        return Opportunity.create(
            symbol=symbol,
            direction=direction,
            as_of=as_of,
            expected_net_return_bps=net,
            confidence=confidence,
            status=status,
            reason=reason,
            opinion_ids=[op.opinion_id for op in group],
        )
