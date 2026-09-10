from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from trading_contracts.schemas.v1 import Direction, MarketRegime, SignalCandidate
from daybagger.engine.risk_gate import RiskDesk, RiskEvaluation
from daybagger.engine.order_desk import PaperOrderDesk


class TestEngineDesks(unittest.TestCase):
    def setUp(self):
        self.risk_desk = RiskDesk(capital=100000.0, risk_per_trade_bps=50.0)

    def test_no_trade_direction_rejected(self):
        now = datetime.now(timezone.utc)
        sig = SignalCandidate(
            signal_id="sig-1",
            instrument_id="RELIANCE",
            created_at=now,
            valid_until=now,
            direction=Direction.NO_TRADE,
            setup_type="CPR_OI",
            regime=MarketRegime.RANGE_BOUND,
            confidence=0.0,
            reason_codes=["MISSING_DATA"],
        )
        res = self.risk_desk.evaluate(sig)
        self.assertFalse(res.approved)
        self.assertEqual(res.rejection_reason, "NO_TRADE_DIRECTION")

    def test_friction_hurdle_failed(self):
        now = datetime.now(timezone.utc)
        # Entry 2500.0, stop 2499.95 -> risk distance 0.05 < 0.10 -> STOP_TOO_TIGHT
        sig = SignalCandidate(
            signal_id="sig-2",
            instrument_id="TCS",
            created_at=now,
            valid_until=now,
            direction=Direction.LONG,
            setup_type="CPR_OI",
            regime=MarketRegime.TRENDING,
            confidence=0.85,
            entry_trigger=2500.0,
            invalidation_level=2499.95,
            reason_codes=["ABOVE_CPR"],
        )
        res = self.risk_desk.evaluate(sig)
        self.assertFalse(res.approved)
        self.assertEqual(res.rejection_reason, "STOP_TOO_TIGHT")

    def test_paper_order_desk_duckdb(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "test_ledger.duckdb")
            desk = PaperOrderDesk(db_path=db_path)
            now = datetime.now(timezone.utc)
            sig = SignalCandidate(
                signal_id="sig-100",
                instrument_id="INFY",
                created_at=now,
                valid_until=now,
                direction=Direction.LONG,
                setup_type="CPR_OI",
                regime=MarketRegime.TRENDING,
                confidence=0.90,
                entry_trigger=1500.0,
                invalidation_level=1490.0,
                reason_codes=["ABOVE_CPR", "OI_LONG_BUILDUP"],
            )
            res = self.risk_desk.evaluate(sig)
            desk.record_signal(sig, res)

            # Check rows in DuckDB
            with desk._get_conn() as conn:
                rows = conn.execute("SELECT * FROM paper_ledger WHERE signal_id = 'sig-100'").fetchall()
                self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
