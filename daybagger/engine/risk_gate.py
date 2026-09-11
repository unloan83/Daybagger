from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
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
    def __init__(self, capital: float = 100000.0, risk_per_trade_bps: float = 50.0):
        self.capital = capital
        self.max_risk_inr = capital * (risk_per_trade_bps / 10000.0)

    def calculate_nse_friction(self, price: float) -> float:
        # Statutory round-trip (STT, GST, Exchange, SEBI, Stamp) + 2 ticks slippage
        slippage = 0.10
        stt = price * 0.00025
        exchange_turnover = price * 0.0000297 * 2
        sebi_charges = price * 0.000001 * 2
        stamp_duty = price * 0.00003
        gst_regulatory = (exchange_turnover + sebi_charges) * 0.18
        return slippage + stt + exchange_turnover + sebi_charges + stamp_duty + gst_regulatory

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
        friction = self.calculate_nse_friction(price)

        if expected_edge < (2.5 * friction):
            return RiskEvaluation(False, f"FRICTION_HURDLE_FAILED (Edge: {expected_edge:.2f} < 2.5x Cost: {2.5*friction:.2f})", 0, friction, expected_edge, stop, target)

        quantity = int(self.max_risk_inr // risk_distance)
        max_notional = self.capital * 5.0
        if (quantity * price) > max_notional:
            quantity = int(max_notional // price)

        if quantity <= 0:
            return RiskEvaluation(False, "INSUFFICIENT_CAPITAL", 0, friction, expected_edge, stop, target)

        return RiskEvaluation(True, None, quantity, friction, expected_edge, stop, target)
