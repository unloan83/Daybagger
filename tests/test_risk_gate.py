from datetime import datetime, timedelta, timezone
import unittest

from daybagger.engine.risk_gate import RiskDesk
from trading_contracts.schemas.v1 import Direction, MarketRegime, SignalCandidate


def _make_signal(
    direction: Direction,
    entry_trigger: float | None,
    invalidation_level: float | None,
) -> SignalCandidate:
    now = datetime.now(timezone.utc)
    return SignalCandidate(
        signal_id=f"test_{direction.value}_{entry_trigger}_{invalidation_level}",
        instrument_id="TCS",
        created_at=now,
        valid_until=now + timedelta(seconds=60),
        direction=direction,
        setup_type="CPR_BREAK",
        regime=MarketRegime.TRENDING,
        confidence=0.85,
        entry_trigger=entry_trigger,
        invalidation_level=invalidation_level,
        reason_codes=["TEST_HARNESS"],
    )


class TestRiskGateGeometry(unittest.TestCase):
    def test_inverted_long_rejected(self):
        result = RiskDesk().evaluate(_make_signal(Direction.LONG, 2500.0, 2510.0))
        assert not result.approved
        assert result.rejection_reason == "GEOMETRY_FAULT_LONG"

    def test_inverted_short_rejected(self):
        result = RiskDesk().evaluate(_make_signal(Direction.SHORT, 3000.0, 2990.0))
        assert not result.approved
        assert result.rejection_reason == "GEOMETRY_FAULT_SHORT"

    def test_missing_price_rejected_cleanly(self):
        result = RiskDesk().evaluate(_make_signal(Direction.LONG, None, None))
        assert not result.approved
        assert result.rejection_reason == "MISSING_PRICE_OR_INVALIDATION"

    def test_non_positive_price_rejected(self):
        result = RiskDesk().evaluate(_make_signal(Direction.LONG, 0.0, 0.0))
        assert not result.approved
        assert result.rejection_reason == "NON_POSITIVE_PRICE_OR_INVALIDATION"

    def test_no_trade_direction_clean_exit(self):
        result = RiskDesk().evaluate(_make_signal(Direction.NO_TRADE, 2500.0, 2480.0))
        assert not result.approved
        assert result.rejection_reason == "NO_TRADE_DIRECTION"

    def test_valid_long_approved(self):
        result = RiskDesk().evaluate(_make_signal(Direction.LONG, 3000.0, 2970.0))
        assert result.approved
        assert result.stop_loss == 2970.0
        assert result.target == 3045.0
        assert result.quantity > 0

    def test_valid_short_approved(self):
        result = RiskDesk().evaluate(_make_signal(Direction.SHORT, 3000.0, 3030.0))
        assert result.approved
        assert result.stop_loss == 3030.0
        assert result.target == 2955.0
        assert result.quantity > 0
