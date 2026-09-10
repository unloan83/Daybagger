import duckdb
import os
from datetime import datetime, timezone
from pathlib import Path
from trading_contracts.schemas.v1 import SignalCandidate, Direction
from daybagger.engine.risk_gate import RiskEvaluation


class PaperOrderDesk:
    def __init__(self, db_path: str = "/home/ubuntu/daybagger/data/paper_ledger.duckdb"):
        if not Path("/home/ubuntu/daybagger").exists():
            db_path = "data/paper_ledger.duckdb"
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        self.active_positions = {}

    def _get_conn(self):
        return duckdb.connect(self.db_path)

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_ledger (
                    timestamp TIMESTAMP,
                    signal_id VARCHAR PRIMARY KEY,
                    instrument_id VARCHAR,
                    direction VARCHAR,
                    status VARCHAR,
                    rejection_reason VARCHAR,
                    entry_price DOUBLE,
                    exit_price DOUBLE,
                    stop_loss DOUBLE,
                    target DOUBLE,
                    quantity INTEGER,
                    pnl DOUBLE,
                    exit_reason VARCHAR
                )
            """)

    def record_signal(self, signal: SignalCandidate, eval_result: RiskEvaluation):
        status = "OPEN" if eval_result.approved else "REJECTED"
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO paper_ledger VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0.0, NULL)
            """, (
                datetime.now(timezone.utc),
                signal.signal_id,
                signal.instrument_id,
                signal.direction.value,
                status,
                eval_result.rejection_reason,
                signal.entry_trigger or 0.0,
                eval_result.stop_loss,
                eval_result.target,
                eval_result.quantity
            ))

        if eval_result.approved:
            self.active_positions[signal.signal_id] = {
                "instrument": signal.instrument_id,
                "direction": signal.direction,
                "entry": signal.entry_trigger,
                "stop": eval_result.stop_loss,
                "target": eval_result.target,
                "qty": eval_result.quantity
            }
            print(f"[ORDER-OPENED] {signal.direction.value} {eval_result.quantity}x {signal.instrument_id} @ ₹{signal.entry_trigger:.2f}")

    def evaluate_open_positions(self, current_prices: dict, current_ist_time: datetime):
        # Enforce 15:15 IST Square-Off
        force_exit = (current_ist_time.hour == 15 and current_ist_time.minute >= 15) or (current_ist_time.hour > 15)

        for sig_id, pos in list(self.active_positions.items()):
            curr = current_prices.get(pos["instrument"])
            if not curr:
                continue

            exit_reason = None
            if force_exit:
                exit_reason = "MIS_SQUARE_OFF_1515"
            elif pos["direction"] == Direction.LONG:
                if curr >= pos["target"]:
                    exit_reason = "TARGET_HIT"
                elif curr <= pos["stop"]:
                    exit_reason = "STOP_LOSS_HIT"
            elif pos["direction"] == Direction.SHORT:
                if curr <= pos["target"]:
                    exit_reason = "TARGET_HIT"
                elif curr >= pos["stop"]:
                    exit_reason = "STOP_LOSS_HIT"

            if exit_reason:
                pnl = (curr - pos["entry"]) * pos["qty"] if pos["direction"] == Direction.LONG else (pos["entry"] - curr) * pos["qty"]
                with self._get_conn() as conn:
                    conn.execute("""
                        UPDATE paper_ledger 
                        SET status = 'CLOSED', exit_price = ?, pnl = ?, exit_reason = ? 
                        WHERE signal_id = ?
                    """, (curr, pnl, exit_reason, sig_id))

                del self.active_positions[sig_id]
                print(f"[POSITION-CLOSED] {pos['instrument']} | Reason: {exit_reason} | Exit: ₹{curr:.2f} | P&L: ₹{pnl:.2f}")
