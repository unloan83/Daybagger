import duckdb
import os
from datetime import datetime, timezone
from pathlib import Path
from trading_contracts.schemas.v1 import SignalCandidate, Direction
from daybagger.engine.risk_gate import RiskEvaluation


class PaperOrderDesk:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = (
                "/home/ubuntu/daybagger/data/paper_ledger.duckdb"
                if Path("/home/ubuntu/daybagger").exists()
                else "data/paper_ledger.duckdb"
            )
        self.db_path = db_path
        db_directory = os.path.dirname(db_path)
        if db_directory:
            os.makedirs(db_directory, exist_ok=True)
        self._init_db()
        self.active_positions = self._load_open_positions()

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

    def _load_open_positions(self):
        """Restore every persisted open position after a listener restart."""
        positions = {}
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT signal_id, instrument_id, direction, entry_price,
                       stop_loss, target, quantity
                FROM paper_ledger
                WHERE status = 'OPEN'
                ORDER BY timestamp, signal_id
            """).fetchall()

        for sig_id, instrument, direction, entry, stop, target, qty in rows:
            try:
                parsed_direction = Direction(direction)
                if not instrument or entry is None or entry <= 0 or qty is None or qty <= 0:
                    raise ValueError("invalid persisted position values")
                if stop is None or target is None:
                    raise ValueError("missing persisted exit levels")
            except (TypeError, ValueError) as exc:
                print(f"[LEDGER-ERROR] Cannot restore open position {sig_id}: {exc}")
                continue

            positions[sig_id] = {
                "instrument": instrument,
                "direction": parsed_direction,
                "entry": entry,
                "stop": stop,
                "target": target,
                "qty": qty,
            }

        if positions:
            print(f"[LEDGER-RECOVERY] Restored {len(positions)} open paper positions")
        return positions

    def record_signal(self, signal: SignalCandidate, eval_result: RiskEvaluation):
        status = "OPEN" if eval_result.approved else "REJECTED"
        rejection_reason = eval_result.rejection_reason
        quantity = eval_result.quantity

        with self._get_conn() as conn:
            # Delivery/replay of the same signal must be a no-op. In particular,
            # never let a replay overwrite a CLOSED row back to OPEN.
            existing_status = conn.execute(
                "SELECT status FROM paper_ledger WHERE signal_id = ?",
                (signal.signal_id,),
            ).fetchone()
            if existing_status:
                print(f"[SIGNAL-REPLAY] Ignored existing signal {signal.signal_id}")
                return existing_status[0]

            # Signal IDs are new on every publisher cycle, so admission must be
            # guarded by the durable instrument state, not only by signal_id.
            if status == "OPEN":
                existing_open = conn.execute("""
                    SELECT 1 FROM paper_ledger
                    WHERE instrument_id = ? AND status = 'OPEN'
                    LIMIT 1
                """, (signal.instrument_id,)).fetchone()
                if existing_open:
                    status = "REJECTED"
                    rejection_reason = "DUPLICATE_OPEN_POSITION"
                    quantity = 0

            conn.execute("""
                INSERT INTO paper_ledger VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0.0, NULL)
            """, (
                datetime.now(timezone.utc),
                signal.signal_id,
                signal.instrument_id,
                signal.direction.value,
                status,
                rejection_reason,
                signal.entry_trigger or 0.0,
                eval_result.stop_loss,
                eval_result.target,
                quantity
            ))

        if status == "OPEN":
            self.active_positions[signal.signal_id] = {
                "instrument": signal.instrument_id,
                "direction": signal.direction,
                "entry": signal.entry_trigger,
                "stop": eval_result.stop_loss,
                "target": eval_result.target,
                "qty": eval_result.quantity
            }
            print(f"[ORDER-OPENED] {signal.direction.value} {eval_result.quantity}x {signal.instrument_id} @ ₹{signal.entry_trigger:.2f}")
        elif rejection_reason == "DUPLICATE_OPEN_POSITION":
            print(f"[ORDER-REJECTED] {signal.instrument_id} | Reason: {rejection_reason}")

        return status

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
