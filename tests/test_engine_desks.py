from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
import duckdb
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

    def _approved_signal(self, signal_id, instrument_id="INFY", direction=Direction.LONG):
        now = datetime.now(timezone.utc)
        entry = 1500.0
        stop = 1490.0 if direction == Direction.LONG else 1510.0
        return SignalCandidate(
            signal_id=signal_id,
            instrument_id=instrument_id,
            created_at=now,
            valid_until=now,
            direction=direction,
            setup_type="CPR_OI",
            regime=MarketRegime.TRENDING,
            confidence=0.90,
            entry_trigger=entry,
            invalidation_level=stop,
            reason_codes=["ABOVE_CPR", "OI_LONG_BUILDUP"],
        )

    def test_duplicate_instrument_is_rejected_persistently(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "test_ledger.duckdb")
            first_desk = PaperOrderDesk(db_path=db_path)
            first = self._approved_signal("sig-first")
            first_desk.record_signal(first, self.risk_desk.evaluate(first))

            # A fresh desk simulates a systemd restart. It must recover the
            # position and reject a new UUID for the same instrument.
            restarted_desk = PaperOrderDesk(db_path=db_path)
            duplicate = self._approved_signal("sig-duplicate", direction=Direction.SHORT)
            status = restarted_desk.record_signal(
                duplicate, self.risk_desk.evaluate(duplicate)
            )

            self.assertEqual(status, "DUPLICATE_DB_LOCKED")
            self.assertIn("sig-first", restarted_desk.active_positions)
            self.assertNotIn("sig-duplicate", restarted_desk.active_positions)
            with restarted_desk._get_conn() as conn:
                row = conn.execute("""
                    SELECT status, rejection_reason, quantity FROM paper_ledger
                    WHERE signal_id = 'sig-duplicate'
                """).fetchone()
                open_count = conn.execute("""
                    SELECT count(*) FROM paper_ledger
                    WHERE instrument_id = 'INFY' AND status = 'OPEN'
                """).fetchone()[0]
            self.assertIsNone(row)
            self.assertEqual(open_count, 1)

    def test_signal_replay_does_not_overwrite_existing_row(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "test_ledger.duckdb")
            desk = PaperOrderDesk(db_path=db_path)
            signal = self._approved_signal("sig-replay")
            evaluation = self.risk_desk.evaluate(signal)
            self.assertEqual(desk.record_signal(signal, evaluation), "OPEN")

            # Close it, then replay the exact payload. INSERT OR REPLACE used
            # to be capable of reverting durable state here.
            desk.evaluate_open_positions(
                {"INFY": evaluation.target},
                datetime.now(timezone.utc),
            )
            self.assertEqual(desk.record_signal(signal, evaluation), "CLOSED")
            with desk._get_conn() as conn:
                row = conn.execute("""
                    SELECT status, count(*) OVER () FROM paper_ledger
                    WHERE signal_id = 'sig-replay'
                """).fetchone()
            self.assertEqual(row, ("CLOSED", 1))

    def test_recovered_position_can_exit_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "test_ledger.duckdb")
            desk = PaperOrderDesk(db_path=db_path)
            signal = self._approved_signal("sig-recovered")
            evaluation = self.risk_desk.evaluate(signal)
            desk.record_signal(signal, evaluation)

            restarted_desk = PaperOrderDesk(db_path=db_path)
            restarted_desk.evaluate_open_positions(
                {"INFY": evaluation.target},
                datetime.now(timezone.utc),
            )
            self.assertNotIn("sig-recovered", restarted_desk.active_positions)
            with restarted_desk._get_conn() as conn:
                row = conn.execute("""
                    SELECT status, exit_reason, exit_timestamp, hold_duration_sec
                    FROM paper_ledger
                    WHERE signal_id = 'sig-recovered'
                """).fetchone()
            self.assertEqual(row[:2], ("CLOSED", "TARGET_HIT"))
            self.assertIsNotNone(row[2])
            self.assertGreaterEqual(row[3], 0.0)

    def test_legacy_ledger_schema_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "legacy_ledger.duckdb")
            with duckdb.connect(db_path) as conn:
                conn.execute("""
                    CREATE TABLE paper_ledger (
                        timestamp TIMESTAMP, signal_id VARCHAR PRIMARY KEY,
                        instrument_id VARCHAR, direction VARCHAR, status VARCHAR,
                        rejection_reason VARCHAR, entry_price DOUBLE,
                        exit_price DOUBLE, stop_loss DOUBLE, target DOUBLE,
                        quantity INTEGER, pnl DOUBLE, exit_reason VARCHAR
                    )
                """)

            PaperOrderDesk(db_path=db_path)
            with duckdb.connect(db_path, read_only=True) as conn:
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info('paper_ledger')"
                    ).fetchall()
                }
            self.assertTrue(
                {"exit_timestamp", "hold_duration_sec", "friction_total"}
                <= columns
            )


if __name__ == "__main__":
    unittest.main()
