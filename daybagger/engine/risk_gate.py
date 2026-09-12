from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Mapping, Optional
from trading_contracts.schemas.v1 import SignalCandidate, Direction


@dataclass
class RiskEvaluation:
    approved: bool
    rejection_reason: Optional[str]
    quantity: int
    friction_per_share: float
    expected_edge_per_share: float
    stop_loss: float
    target: float


@dataclass(frozen=True)
class PortfolioRiskLimits:
    max_open_positions: int = 4
    max_aggregate_open_risk_inr: float = 1000.0
    max_gross_notional_inr: float = 200000.0
    hard_daily_loss_limit_inr: float = 1000.0
    max_sector_risk_inr: float = 750.0
    max_sector_notional_inr: float = 100000.0
    correlation_threshold: float = 0.75
    max_correlated_risk_inr: float = 750.0
    max_correlated_positions: int = 2

    def __post_init__(self):
        positive = (
            self.max_open_positions,
            self.max_aggregate_open_risk_inr,
            self.max_gross_notional_inr,
            self.hard_daily_loss_limit_inr,
            self.max_sector_risk_inr,
            self.max_sector_notional_inr,
            self.max_correlated_risk_inr,
            self.max_correlated_positions,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("portfolio risk limits must be positive")
        if not 0.0 <= self.correlation_threshold <= 1.0:
            raise ValueError("correlation_threshold must be in [0,1]")


@dataclass(frozen=True)
class OpenPositionExposure:
    instrument_id: str
    sector: str
    risk_inr: float
    notional_inr: float


@dataclass(frozen=True)
class PortfolioState:
    positions: tuple[OpenPositionExposure, ...]
    daily_net_pnl_inr: float

    @property
    def open_risk_inr(self) -> float:
        return sum(item.risk_inr for item in self.positions)

    @property
    def gross_notional_inr(self) -> float:
        return sum(item.notional_inr for item in self.positions)


class PortfolioRiskGate:
    """Portfolio admission without changing the underlying signal geometry."""

    def __init__(self, limits: PortfolioRiskLimits | None = None):
        self.limits = limits or PortfolioRiskLimits()

    def rejection_reason(
        self,
        *,
        instrument_id: str,
        sector: str,
        candidate_risk_inr: float,
        candidate_notional_inr: float,
        state: PortfolioState,
        correlations: Mapping[str, float | None],
    ) -> str | None:
        limits = self.limits
        if candidate_risk_inr <= 0 or candidate_notional_inr <= 0:
            return "INVALID_CANDIDATE_EXPOSURE"
        if state.daily_net_pnl_inr <= -limits.hard_daily_loss_limit_inr:
            return "DAILY_LOSS_LIMIT_REACHED"
        if len(state.positions) >= limits.max_open_positions:
            return "MAX_OPEN_POSITIONS_REACHED"
        if state.open_risk_inr + candidate_risk_inr > limits.max_aggregate_open_risk_inr:
            return "AGGREGATE_OPEN_RISK_LIMIT_REACHED"
        if state.gross_notional_inr + candidate_notional_inr > limits.max_gross_notional_inr:
            return "GROSS_NOTIONAL_LIMIT_REACHED"

        sector_positions = [item for item in state.positions if item.sector == sector]
        if sum(item.risk_inr for item in sector_positions) + candidate_risk_inr > limits.max_sector_risk_inr:
            return "SECTOR_RISK_LIMIT_REACHED"
        if sum(item.notional_inr for item in sector_positions) + candidate_notional_inr > limits.max_sector_notional_inr:
            return "SECTOR_NOTIONAL_LIMIT_REACHED"

        correlated = [
            item
            for item in state.positions
            if correlations.get(item.instrument_id) is not None
            and abs(float(correlations[item.instrument_id])) >= limits.correlation_threshold
        ]
        if len(correlated) + 1 > limits.max_correlated_positions:
            return "CORRELATED_POSITION_COUNT_LIMIT_REACHED"
        if sum(item.risk_inr for item in correlated) + candidate_risk_inr > limits.max_correlated_risk_inr:
            return "CORRELATED_RISK_LIMIT_REACHED"
        return None


def validate_signal_geometry(signal: SignalCandidate) -> Optional[tuple[bool, str]]:
    """Return a deterministic rejection when entry/stop geometry is invalid."""
    if signal.direction == Direction.NO_TRADE:
        return False, "NO_TRADE_DIRECTION"

    entry = signal.entry_trigger
    stop = signal.invalidation_level
    if entry is None or stop is None:
        return False, "MISSING_PRICE_OR_INVALIDATION"
    if entry <= 0 or stop <= 0:
        return False, "NON_POSITIVE_PRICE_OR_INVALIDATION"
    if signal.direction == Direction.LONG and stop >= entry:
        return False, "GEOMETRY_FAULT_LONG"
    if signal.direction == Direction.SHORT and stop <= entry:
        return False, "GEOMETRY_FAULT_SHORT"
    return None


class RiskDesk:
    def __init__(
        self,
        capital: float = 100000.0,
        risk_per_trade_bps: float = 50.0,
        slippage_bps_per_side: float = 2.0,
    ):
        self.capital = capital
        self.max_risk_inr = capital * (risk_per_trade_bps / 10000.0)
        self.slippage_bps_per_side = slippage_bps_per_side

    def calculate_nse_friction(
        self, price: float, slippage_bps_per_side: float | None = None
    ) -> float:
        # The same conservative percentage regime used by paper accounting and
        # historical validation. Actual quantity-level costs are recomputed on exit.
        from daybagger.integration.costs import IndiaEquityIntradayCostModel

        model = IndiaEquityIntradayCostModel()
        total_bps = (
            model.conservative_linear_round_trip_bps()
            + 2.0 * (
                self.slippage_bps_per_side
                if slippage_bps_per_side is None
                else slippage_bps_per_side
            )
        )
        return price * total_bps / 10000.0

    def evaluate(self, signal: SignalCandidate) -> RiskEvaluation:
        fault = validate_signal_geometry(signal)
        if fault:
            approved, reason = fault
            return RiskEvaluation(approved, reason, 0, 0.0, 0.0, 0.0, 0.0)

        # Geometry validation above establishes positive, non-null prices.
        price = signal.entry_trigger
        stop = signal.invalidation_level

        risk_distance = abs(price - stop)
        if risk_distance < 0.10:
            return RiskEvaluation(False, "STOP_TOO_TIGHT", 0, 0, 0, 0, 0)

        target = price + (1.5 * risk_distance) if signal.direction == Direction.LONG else price - (1.5 * risk_distance)
        expected_edge = abs(target - price)
        friction = self.calculate_nse_friction(
            price, signal.slippage_bps_per_side
        )

        if expected_edge < (2.5 * friction):
            return RiskEvaluation(False, f"FRICTION_HURDLE_FAILED (Edge: {expected_edge:.2f} < 2.5x Cost: {2.5*friction:.2f})", 0, friction, expected_edge, stop, target)

        quantity = int(self.max_risk_inr // risk_distance)
        max_notional = self.capital * 5.0
        if (quantity * price) > max_notional:
            quantity = int(max_notional // price)

        if quantity <= 0:
            return RiskEvaluation(False, "INSUFFICIENT_CAPITAL", 0, friction, expected_edge, stop, target)

        return RiskEvaluation(True, None, quantity, friction, expected_edge, stop, target)
