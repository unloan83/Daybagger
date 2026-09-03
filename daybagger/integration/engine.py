from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from daybagger.decision.ensemble import EvidenceWeightedEnsemble
from daybagger.decision.model import ValidatedLinearModel, ValidatedModelSpec
from daybagger.decision.risk import AdaptiveCapitalAllocator, AllocationDecision, CapitalState
from daybagger.domain import DecisionStatus, ModelOpinion, Opportunity
from daybagger.integration.costs import IndiaEquityIntradayCostModel


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    symbol: str
    as_of: datetime
    opinions: tuple[ModelOpinion, ...]
    opportunity: Opportunity
    allocation: AllocationDecision
    estimated_cost_bps: float
    model_validation_ids: Mapping[str, str]


class CanonicalDecisionEngine:
    """
    The one Daybagger decision path.

    Features -> validated specialists -> evidence weights -> net-edge ensemble
    -> opportunity -> adaptive capital allocation.

    It does not place an order.
    """

    def __init__(
        self,
        *,
        specs: Sequence[ValidatedModelSpec],
        model_weights: Mapping[str, float],
        allocator: AdaptiveCapitalAllocator,
        cost_model: IndiaEquityIntradayCostModel | None = None,
    ):
        self.specs = tuple(specs)
        self.model_weights = dict(model_weights)
        self.allocator = allocator
        self.cost_model = cost_model or IndiaEquityIntradayCostModel()
        self.ensemble = EvidenceWeightedEnsemble()

    def decide(
        self,
        *,
        symbol: str,
        as_of: datetime,
        features: Mapping[str, float],
        evidence_ids: Sequence,
        capital: CapitalState,
        estimated_volatility_bps: float,
        reference_price: Decimal,
        reference_quantity: int,
    ) -> DecisionTrace:
        if reference_price <= 0 or reference_quantity <= 0:
            raise ValueError("positive reference price/quantity are required")

        turnover = reference_price * Decimal(reference_quantity)
        costs = self.cost_model.estimate_round_trip(
            buy_turnover=turnover,
            sell_turnover=turnover,
        )
        cost_bps = costs.total_bps(turnover, turnover)

        opinions: list[ModelOpinion] = []
        validation_ids: dict[str, str] = {}
        for spec in self.specs:
            model = ValidatedLinearModel(spec)
            try:
                opinion = model.evaluate(
                    symbol=symbol,
                    as_of=as_of,
                    features=features,
                    evidence_ids=evidence_ids,
                )
            except Exception:
                # Missing specialist-specific evidence is fail-closed for that specialist,
                # not a reason to fabricate a default value.
                continue
            if opinion is not None:
                opinions.append(opinion)
                validation_ids[spec.model_id] = spec.validation_id

        opportunity = self.ensemble.rank(
            symbol=symbol,
            as_of=as_of,
            opinions=opinions,
            model_weights=self.model_weights,
            estimated_round_trip_cost_bps=cost_bps,
        )

        allocation = self.allocator.allocate(
            opportunity=opportunity,
            capital=capital,
            estimated_volatility_bps=estimated_volatility_bps,
        )

        return DecisionTrace(
            symbol=symbol,
            as_of=as_of,
            opinions=tuple(opinions),
            opportunity=opportunity,
            allocation=allocation,
            estimated_cost_bps=cost_bps,
            model_validation_ids=validation_ids,
        )
