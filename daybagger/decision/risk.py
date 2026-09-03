from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from daybagger.domain import DecisionStatus, Opportunity


class AllocationError(RuntimeError):
    """Capital cannot be allocated safely."""


@dataclass(frozen=True, slots=True)
class CapitalState:
    equity_inr: Decimal
    available_cash_inr: Decimal
    peak_equity_inr: Decimal
    open_risk_inr: Decimal = Decimal("0")

    @property
    def drawdown_fraction(self) -> float:
        if self.peak_equity_inr <= 0:
            return 0.0
        drawdown = max(
            Decimal("0"),
            self.peak_equity_inr - self.equity_inr,
        )
        return float(drawdown / self.peak_equity_inr)


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    approved: bool
    reason: str
    capital_inr: Decimal
    max_loss_inr: Decimal
    risk_fraction: float


class AdaptiveCapitalAllocator:
    """
    Relative, opportunity-aware allocation without martingale behaviour.

    Configuration is explicit and must be chosen/validated outside this class.
    Drawdown can only REDUCE risk, never increase it to chase losses.
    """

    def __init__(
        self,
        *,
        base_risk_fraction: float,
        max_risk_fraction: float,
        max_position_fraction: float,
    ):
        if not (0 < base_risk_fraction <= max_risk_fraction < 1):
            raise AllocationError("invalid risk fractions")
        if not (0 < max_position_fraction <= 1):
            raise AllocationError("max_position_fraction must be in (0,1]")
        self.base_risk_fraction = base_risk_fraction
        self.max_risk_fraction = max_risk_fraction
        self.max_position_fraction = max_position_fraction

    def allocate(
        self,
        *,
        opportunity: Opportunity,
        capital: CapitalState,
        estimated_volatility_bps: float,
    ) -> AllocationDecision:
        if opportunity.status != DecisionStatus.QUALIFIED:
            return AllocationDecision(
                approved=False,
                reason="OPPORTUNITY_NOT_QUALIFIED",
                capital_inr=Decimal("0"),
                max_loss_inr=Decimal("0"),
                risk_fraction=0.0,
            )
        if capital.equity_inr <= 0 or capital.available_cash_inr <= 0:
            return AllocationDecision(False, "NO_AVAILABLE_CAPITAL", Decimal("0"), Decimal("0"), 0.0)
        if estimated_volatility_bps <= 0:
            return AllocationDecision(False, "INVALID_VOLATILITY", Decimal("0"), Decimal("0"), 0.0)

        edge_to_vol = max(
            0.0,
            opportunity.expected_net_return_bps / estimated_volatility_bps,
        )
        quality = max(0.0, min(1.0, opportunity.confidence * edge_to_vol))

        # Drawdown decreases deployment. It never increases size after losses.
        drawdown_factor = max(0.25, 1.0 - capital.drawdown_fraction)
        risk_fraction = min(
            self.max_risk_fraction,
            self.base_risk_fraction * (0.5 + quality) * drawdown_factor,
        )

        max_loss = capital.equity_inr * Decimal(str(risk_fraction))
        deployable = min(
            capital.available_cash_inr,
            capital.equity_inr * Decimal(str(self.max_position_fraction)),
        )

        if deployable <= 0 or max_loss <= 0:
            return AllocationDecision(False, "ZERO_DEPLOYABLE_CAPITAL", Decimal("0"), Decimal("0"), 0.0)

        return AllocationDecision(
            approved=True,
            reason="ALLOCATED_FROM_EDGE_CONFIDENCE_VOLATILITY_AND_DRAWDOWN",
            capital_inr=deployable,
            max_loss_inr=max_loss,
            risk_fraction=risk_fraction,
        )
