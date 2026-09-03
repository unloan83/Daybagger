from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from daybagger.domain import DecisionStatus, Direction, ExecutableQuote, Opportunity
from daybagger.integration.costs import IndiaEquityIntradayCostModel


class AllocationError(RuntimeError):
    """Capital cannot be allocated safely."""


@dataclass(frozen=True, slots=True)
class CapitalState:
    equity_inr: Decimal
    available_cash_inr: Decimal
    peak_equity_inr: Decimal
    open_risk_inr: Decimal = Decimal("0")
    daily_net_pnl_inr: Decimal = Decimal("0")

    @property
    def drawdown_fraction(self) -> float:
        if self.peak_equity_inr <= 0:
            return 0.0
        drawdown = max(
            Decimal("0"),
            self.peak_equity_inr - self.equity_inr,
        )
        return float(drawdown / self.peak_equity_inr)

    def reserve(self, *, capital_inr: Decimal, risk_inr: Decimal) -> "CapitalState":
        if capital_inr < 0 or risk_inr < 0:
            raise AllocationError("capital/risk reservation cannot be negative")
        if capital_inr > self.available_cash_inr:
            raise AllocationError("capital reservation exceeds available cash")
        return CapitalState(
            equity_inr=self.equity_inr,
            available_cash_inr=self.available_cash_inr - capital_inr,
            peak_equity_inr=self.peak_equity_inr,
            open_risk_inr=self.open_risk_inr + risk_inr,
            daily_net_pnl_inr=self.daily_net_pnl_inr,
        )


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    approved: bool
    reason: str
    capital_inr: Decimal
    max_loss_inr: Decimal
    risk_fraction: float


@dataclass(frozen=True, slots=True)
class ExecutableSizingDecision:
    approved: bool
    reason: str
    quantity: int
    entry_price: Decimal
    entry_notional_inr: Decimal
    estimated_adverse_loss_inr: Decimal
    estimated_round_trip_cost_inr: Decimal


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
        hard_daily_loss_limit_inr: Decimal | None = None,
        max_aggregate_open_risk_inr: Decimal | None = None,
    ):
        if not (0 < base_risk_fraction <= max_risk_fraction < 1):
            raise AllocationError("invalid risk fractions")
        if not (0 < max_position_fraction <= 1):
            raise AllocationError("max_position_fraction must be in (0,1]")
        if hard_daily_loss_limit_inr is not None and hard_daily_loss_limit_inr <= 0:
            raise AllocationError("hard_daily_loss_limit_inr must be positive")
        if max_aggregate_open_risk_inr is not None and max_aggregate_open_risk_inr <= 0:
            raise AllocationError("max_aggregate_open_risk_inr must be positive")
        self.base_risk_fraction = base_risk_fraction
        self.max_risk_fraction = max_risk_fraction
        self.max_position_fraction = max_position_fraction
        self.hard_daily_loss_limit_inr = hard_daily_loss_limit_inr
        self.max_aggregate_open_risk_inr = max_aggregate_open_risk_inr

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
        if (
            self.hard_daily_loss_limit_inr is not None
            and capital.daily_net_pnl_inr <= -self.hard_daily_loss_limit_inr
        ):
            return AllocationDecision(
                False,
                "DAILY_LOSS_LIMIT_REACHED",
                Decimal("0"),
                Decimal("0"),
                0.0,
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

        # Drawdown decreases deployment continuously to zero. There is deliberately
        # no 25% floor that would force continued risk during catastrophic loss.
        drawdown_factor = max(0.0, 1.0 - capital.drawdown_fraction)
        risk_fraction = min(
            self.max_risk_fraction,
            self.base_risk_fraction * (0.5 + quality) * drawdown_factor,
        )

        max_loss = capital.equity_inr * Decimal(str(risk_fraction))
        if self.max_aggregate_open_risk_inr is not None:
            remaining_open_risk = (
                self.max_aggregate_open_risk_inr - capital.open_risk_inr
            )
            if remaining_open_risk <= 0:
                return AllocationDecision(
                    False,
                    "AGGREGATE_OPEN_RISK_LIMIT_REACHED",
                    Decimal("0"),
                    Decimal("0"),
                    0.0,
                )
            max_loss = min(max_loss, remaining_open_risk)
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


class ExecutionSizer:
    """
    Convert an approved allocation into an integer executable quantity.

    Risk and charges are checked at the ACTUAL quantity. Full notional is used
    for both LONG and SHORT paper sizing, which is conservative relative to MIS
    margin and prevents hidden leverage from entering the decision path.
    """

    def __init__(self, cost_model: IndiaEquityIntradayCostModel | None = None):
        self.cost_model = cost_model or IndiaEquityIntradayCostModel()

    def size(
        self,
        *,
        opportunity: Opportunity,
        allocation: AllocationDecision,
        quote: ExecutableQuote,
        estimated_volatility_bps: float,
        slippage_bps: float = 0.0,
    ) -> ExecutableSizingDecision:
        quote.validate()
        if opportunity.status != DecisionStatus.QUALIFIED or not allocation.approved:
            return _no_size("OPPORTUNITY_OR_ALLOCATION_NOT_APPROVED")
        if estimated_volatility_bps <= 0:
            return _no_size("INVALID_VOLATILITY")
        if slippage_bps < 0:
            raise AllocationError("slippage_bps cannot be negative")

        if opportunity.direction == Direction.LONG:
            entry_price = quote.ask
        elif opportunity.direction == Direction.SHORT:
            entry_price = quote.bid
        else:
            return _no_size("FLAT_NOT_EXECUTABLE")

        qty_by_capital = int(
            (allocation.capital_inr / entry_price).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        adverse_per_share = (
            entry_price
            * Decimal(str(estimated_volatility_bps))
            / Decimal("10000")
        )
        if adverse_per_share <= 0:
            return _no_size("NON_POSITIVE_RISK_PER_SHARE")
        qty_by_price_risk = int(
            (allocation.max_loss_inr / adverse_per_share).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        quantity = min(qty_by_capital, qty_by_price_risk)
        if quantity <= 0:
            return _no_size("ZERO_EXECUTABLE_QUANTITY")

        # Costs are nonlinear around the brokerage cap, so solve using the actual
        # quantity and shrink conservatively if the total adverse-loss budget is
        # exceeded. This loop only decreases quantity and is deterministically bounded.
        for _ in range(4):
            buy_turnover = quote.ask * Decimal(quantity)
            sell_turnover = quote.bid * Decimal(quantity)
            costs = self.cost_model.estimate_round_trip(
                buy_turnover=buy_turnover,
                sell_turnover=sell_turnover,
            )
            slippage = (
                (buy_turnover + sell_turnover)
                * Decimal(str(slippage_bps))
                / Decimal("10000")
            )
            round_trip_cost = costs.total + slippage
            adverse_loss = adverse_per_share * Decimal(quantity)
            total_risk = adverse_loss + round_trip_cost
            if total_risk <= allocation.max_loss_inr:
                notional = entry_price * Decimal(quantity)
                return ExecutableSizingDecision(
                    approved=True,
                    reason="SIZED_FROM_ACTUAL_PRICE_RISK_AND_COSTS",
                    quantity=quantity,
                    entry_price=entry_price,
                    entry_notional_inr=notional,
                    estimated_adverse_loss_inr=adverse_loss,
                    estimated_round_trip_cost_inr=round_trip_cost,
                )
            scaled = int(
                (
                    Decimal(quantity)
                    * allocation.max_loss_inr
                    / total_risk
                ).to_integral_value(rounding=ROUND_DOWN)
            )
            quantity = min(quantity - 1, scaled)
            if quantity <= 0:
                break
        return _no_size("RISK_BUDGET_CANNOT_SUPPORT_ONE_SHARE")


def _no_size(reason: str) -> ExecutableSizingDecision:
    return ExecutableSizingDecision(
        approved=False,
        reason=reason,
        quantity=0,
        entry_price=Decimal("0"),
        entry_notional_inr=Decimal("0"),
        estimated_adverse_loss_inr=Decimal("0"),
        estimated_round_trip_cost_inr=Decimal("0"),
    )
