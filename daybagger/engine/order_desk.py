from __future__ import annotations

import os
import time
from datetime import datetime, time as clock_time, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
from trading_contracts.schemas.v1 import Direction, SignalCandidate

from daybagger.engine.risk_gate import (
    OpenPositionExposure,
    PortfolioRiskGate,
    PortfolioState,
    RiskEvaluation,
)
from daybagger.engine.risk_metadata import InstrumentRiskMetadata, RiskMetadataError
from daybagger.integration.costs import IndiaEquityIntradayCostModel


IST = ZoneInfo("Asia/Kolkata")


class PaperOrderDesk:
    """Durable paper order ledger with atomic portfolio admission."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        risk_metadata: InstrumentRiskMetadata | None = None,
        risk_metadata_path: str | Path | None = None,
        portfolio_gate: PortfolioRiskGate | None = None,
        slippage_bps_per_side: float = 2.0,
        transaction_retries: int = 20,
    ):
        if db_path is None:
            db_path = (
                "/home/ubuntu/daybagger/data/paper_ledger.duckdb"
                if Path("/home/ubuntu/daybagger").exists()
                else "data/paper_ledger.duckdb"
            )
        if slippage_bps_per_side < 0:
            raise ValueError("slippage_bps_per_side cannot be negative")
        if transaction_retries <= 0:
            raise ValueError("transaction_retries must be positive")

        self.db_path = db_path
        self.portfolio_gate = portfolio_gate or PortfolioRiskGate()
        self.cost_model = IndiaEquityIntradayCostModel()
        self.slippage_bps_per_side = slippage_bps_per_side
        self.transaction_retries = transaction_retries

        if risk_metadata is not None:
            self.risk_metadata = risk_metadata
        else:
            metadata_path = Path(
                risk_metadata_path
                or os.getenv(
                    "DAYBAGGER_RISK_METADATA",
                    "config/instrument_risk_metadata.json",
                )
            )
            self.risk_metadata = InstrumentRiskMetadata.load(metadata_path)

        db_directory = os.path.dirname(db_path)
        if db_directory:
            os.makedirs(db_directory, exist_ok=True)
        self._init_db()
        self.active_positions = self._hydrate_active_state()

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
                    exit_reason VARCHAR,
                    exit_timestamp TIMESTAMP,
                    hold_duration_sec DOUBLE,
                    friction_total DOUBLE,
                    sector VARCHAR,
                    risk_amount DOUBLE,
                    entry_notional DOUBLE,
                    gross_pnl DOUBLE,
                    modeled_costs DOUBLE,
                    modeled_slippage DOUBLE,
                    net_pnl DOUBLE,
                    slippage_bps_per_side DOUBLE,
                    cohort VARCHAR
                )
            """)
            migrations = {
                "exit_timestamp": "TIMESTAMP",
                "hold_duration_sec": "DOUBLE",
                "friction_total": "DOUBLE",
                "sector": "VARCHAR",
                "risk_amount": "DOUBLE",
                "entry_notional": "DOUBLE",
                "gross_pnl": "DOUBLE",
                "modeled_costs": "DOUBLE",
                "modeled_slippage": "DOUBLE",
                "net_pnl": "DOUBLE",
                "slippage_bps_per_side": "DOUBLE",
                "cohort": "VARCHAR",
            }
            for column, column_type in migrations.items():
                conn.execute(
                    f"ALTER TABLE paper_ledger ADD COLUMN IF NOT EXISTS {column} {column_type}"
                )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS open_instrument_locks (
                    instrument_id VARCHAR PRIMARY KEY,
                    signal_id VARCHAR UNIQUE NOT NULL,
                    acquired_at TIMESTAMP NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_admission_mutex (
                    mutex_id INTEGER PRIMARY KEY,
                    version BIGINT NOT NULL,
                    CHECK (mutex_id = 1)
                )
            """)
            conn.execute("""
                INSERT INTO portfolio_admission_mutex
                SELECT 1, 0
                WHERE NOT EXISTS (
                    SELECT 1 FROM portfolio_admission_mutex WHERE mutex_id = 1
                )
            """)

            duplicates = conn.execute("""
                SELECT instrument_id, COUNT(*)
                FROM paper_ledger
                WHERE status = 'OPEN'
                GROUP BY instrument_id
                HAVING COUNT(*) > 1
            """).fetchall()
            if duplicates:
                raise RuntimeError(
                    "cannot establish atomic locks: duplicate OPEN instruments exist"
                )
            conn.execute("""
                INSERT INTO open_instrument_locks
                SELECT instrument_id, signal_id, timestamp
                FROM paper_ledger p
                WHERE status = 'OPEN'
                  AND NOT EXISTS (
                      SELECT 1 FROM open_instrument_locks l
                      WHERE l.instrument_id = p.instrument_id
                  )
            """)

        self._backfill_accounting()

    def _backfill_accounting(self) -> None:
        """Idempotently give legacy closed rows canonical gross/cost/net fields."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT signal_id, status, entry_price, exit_price, quantity, pnl
                FROM paper_ledger
                WHERE net_pnl IS NULL
                ORDER BY timestamp, signal_id
            """).fetchall()
            for signal_id, status, entry, exit_price, qty, legacy_pnl in rows:
                if status == "REJECTED":
                    conn.execute("""
                        UPDATE paper_ledger
                        SET gross_pnl = 0.0, modeled_costs = 0.0,
                            modeled_slippage = 0.0, net_pnl = 0.0,
                            friction_total = 0.0
                        WHERE signal_id = ?
                    """, (signal_id,))
                    continue
                if status != "CLOSED" or not entry or not exit_price or not qty:
                    continue
                friction = self.cost_model.estimate_trade_friction(
                    entry_price=Decimal(str(entry)),
                    exit_price=Decimal(str(exit_price)),
                    quantity=int(qty),
                    slippage_bps_per_side=self.slippage_bps_per_side,
                )
                gross = float(legacy_pnl or 0.0)
                net = gross - float(friction.total)
                conn.execute("""
                    UPDATE paper_ledger
                    SET gross_pnl = ?, modeled_costs = ?, modeled_slippage = ?,
                        net_pnl = ?, friction_total = ?
                    WHERE signal_id = ?
                """, (
                    gross,
                    float(friction.statutory_and_brokerage),
                    float(friction.slippage),
                    net,
                    float(friction.total),
                    signal_id,
                ))

    def _hydrate_active_state(self):
        """Restore every persisted open position after a listener restart."""
        positions = {}
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT signal_id, instrument_id, direction, entry_price,
                       stop_loss, target, quantity, timestamp, friction_total,
                       sector, risk_amount, entry_notional
                       , slippage_bps_per_side, cohort
                FROM paper_ledger
                WHERE status = 'OPEN'
                ORDER BY timestamp, signal_id
            """).fetchall()

        for row in rows:
            (
                sig_id, instrument, direction, entry, stop, target, qty,
                opened_at, friction, sector, risk_amount, entry_notional,
                slippage_bps_per_side, cohort,
            ) = row
            try:
                parsed_direction = Direction(direction)
                if not instrument or entry is None or entry <= 0 or qty is None or qty <= 0:
                    raise ValueError("invalid persisted position values")
                if stop is None or target is None:
                    raise ValueError("missing persisted exit levels")
                if opened_at is None:
                    raise ValueError("missing persisted entry timestamp")
                profile = self.risk_metadata.profile(instrument)
                resolved_sector = sector or profile.sector
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=timezone.utc)
                else:
                    opened_at = opened_at.astimezone(timezone.utc)
            except (TypeError, ValueError, RiskMetadataError) as exc:
                raise RuntimeError(
                    f"cannot safely restore open position {sig_id}: {exc}"
                ) from exc

            positions[sig_id] = {
                "instrument": instrument,
                "direction": parsed_direction,
                "entry": entry,
                "stop": stop,
                "target": target,
                "qty": qty,
                "opened_at": opened_at,
                "friction_total": friction or 0.0,
                "sector": resolved_sector,
                "risk_amount": risk_amount or abs(entry - stop) * qty,
                "entry_notional": entry_notional or entry * qty,
                "slippage_bps_per_side": (
                    self.slippage_bps_per_side
                    if slippage_bps_per_side is None
                    else slippage_bps_per_side
                ),
                "cohort": cohort or "STANDARD",
            }

        if positions:
            print(f"[LEDGER-RECOVERY] Restored {len(positions)} open paper positions")
        return positions

    def record_signal(self, signal: SignalCandidate, eval_result: RiskEvaluation):
        opened_at = datetime.now(timezone.utc)
        approved = bool(eval_result.approved)
        rejection_reason = eval_result.rejection_reason
        profile = None
        if approved:
            try:
                profile = self.risk_metadata.profile(signal.instrument_id)
            except RiskMetadataError:
                approved = False
                rejection_reason = "RISK_METADATA_MISSING"

        for attempt in range(self.transaction_retries):
            try:
                result = self._record_signal_transaction(
                    signal=signal,
                    eval_result=eval_result,
                    approved=approved,
                    rejection_reason=rejection_reason,
                    profile=profile,
                    opened_at=opened_at,
                )
                break
            except duckdb.Error as exc:
                if not _is_retryable_db_contention(exc):
                    raise
                if attempt + 1 >= self.transaction_retries:
                    raise
                time.sleep(min(0.001 * (2 ** attempt), 0.05))
        else:  # pragma: no cover
            raise RuntimeError("portfolio admission retry loop exhausted")

        status, final_reason, quantity, friction_total = result
        if status == "OPEN":
            self.active_positions[signal.signal_id] = {
                "instrument": signal.instrument_id,
                "direction": signal.direction,
                "entry": signal.entry_trigger,
                "stop": eval_result.stop_loss,
                "target": eval_result.target,
                "qty": quantity,
                "opened_at": opened_at,
                "friction_total": friction_total,
                "sector": profile.sector,
                "risk_amount": abs(signal.entry_trigger - eval_result.stop_loss) * quantity,
                "entry_notional": signal.entry_trigger * quantity,
                "slippage_bps_per_side": (
                    self.slippage_bps_per_side
                    if signal.slippage_bps_per_side is None
                    else signal.slippage_bps_per_side
                ),
                "cohort": signal.cohort,
            }
            print(
                f"[ORDER-OPENED] {signal.direction.value} {quantity}x "
                f"{signal.instrument_id} @ ₹{signal.entry_trigger:.2f}"
            )
        elif status == "DUPLICATE_DB_LOCKED":
            print(f"[DUPLICATE_DB_LOCKED] {signal.instrument_id} already OPEN")
        elif final_reason:
            print(f"[ORDER-REJECTED] {signal.instrument_id} | Reason: {final_reason}")
        return status

    def _record_signal_transaction(
        self,
        *,
        signal: SignalCandidate,
        eval_result: RiskEvaluation,
        approved: bool,
        rejection_reason: str | None,
        profile,
        opened_at: datetime,
    ) -> tuple[str, str | None, int, float]:
        conn = self._get_conn()
        try:
            conn.execute("BEGIN TRANSACTION")
            # Every admission writes the same mutex row. A concurrent writer is
            # aborted by DuckDB and retried, so portfolio reads cannot both admit.
            conn.execute("""
                UPDATE portfolio_admission_mutex
                SET version = version + 1
                WHERE mutex_id = 1
            """)

            existing_status = conn.execute(
                "SELECT status FROM paper_ledger WHERE signal_id = ?",
                (signal.signal_id,),
            ).fetchone()
            if existing_status:
                conn.execute("COMMIT")
                print(f"[SIGNAL-REPLAY] Ignored existing signal {signal.signal_id}")
                return existing_status[0], None, 0, 0.0

            quantity = eval_result.quantity if approved else 0
            final_reason = rejection_reason
            candidate_risk = 0.0
            candidate_notional = 0.0
            friction_total = 0.0

            if approved:
                try:
                    conn.execute("""
                        INSERT INTO open_instrument_locks
                        VALUES (?, ?, ?)
                    """, (signal.instrument_id, signal.signal_id, opened_at))
                except duckdb.ConstraintException:
                    conn.execute("ROLLBACK")
                    return "DUPLICATE_DB_LOCKED", "DUPLICATE_DB_LOCKED", 0, 0.0

                state = self._portfolio_state(conn, opened_at)
                correlations: dict[str, float | None] = {}
                missing_correlation = False
                for position in state.positions:
                    value = self.risk_metadata.correlation(
                        signal.instrument_id, position.instrument_id
                    )
                    correlations[position.instrument_id] = value
                    if value is None:
                        missing_correlation = True

                candidate_risk = (
                    abs(signal.entry_trigger - eval_result.stop_loss) * quantity
                )
                candidate_notional = signal.entry_trigger * quantity
                if missing_correlation:
                    final_reason = "CORRELATION_METADATA_MISSING"
                else:
                    final_reason = self.portfolio_gate.rejection_reason(
                        instrument_id=signal.instrument_id,
                        sector=profile.sector,
                        candidate_risk_inr=candidate_risk,
                        candidate_notional_inr=candidate_notional,
                        state=state,
                        correlations=correlations,
                    )
                if final_reason:
                    approved = False
                    quantity = 0
                    conn.execute(
                        "DELETE FROM open_instrument_locks WHERE instrument_id = ?",
                        (signal.instrument_id,),
                    )
                else:
                    signal_slippage = (
                        self.slippage_bps_per_side
                        if signal.slippage_bps_per_side is None
                        else signal.slippage_bps_per_side
                    )
                    friction = self.cost_model.estimate_trade_friction(
                        entry_price=Decimal(str(signal.entry_trigger)),
                        exit_price=Decimal(str(signal.entry_trigger)),
                        quantity=quantity,
                        slippage_bps_per_side=signal_slippage,
                    )
                    friction_total = float(friction.total)

            status = "OPEN" if approved else "REJECTED"
            conn.execute("""
                INSERT INTO paper_ledger (
                    timestamp, signal_id, instrument_id, direction, status,
                    rejection_reason, entry_price, exit_price, stop_loss,
                    target, quantity, pnl, exit_reason, exit_timestamp,
                    hold_duration_sec, friction_total, sector, risk_amount,
                    entry_notional, gross_pnl, modeled_costs,
                    modeled_slippage, net_pnl, slippage_bps_per_side, cohort
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0.0, NULL,
                          NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
            """, (
                opened_at,
                signal.signal_id,
                signal.instrument_id,
                signal.direction.value,
                status,
                final_reason,
                signal.entry_trigger or 0.0,
                eval_result.stop_loss,
                eval_result.target,
                quantity,
                friction_total,
                profile.sector if profile else None,
                candidate_risk if approved else 0.0,
                candidate_notional if approved else 0.0,
                (
                    self.slippage_bps_per_side
                    if signal.slippage_bps_per_side is None
                    else signal.slippage_bps_per_side
                ),
                signal.cohort,
            ))
            conn.execute("COMMIT")
            return status, final_reason, quantity, friction_total
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _portfolio_state(self, conn, now_utc: datetime) -> PortfolioState:
        rows = conn.execute("""
            SELECT instrument_id, sector, risk_amount, entry_notional,
                   entry_price, stop_loss, quantity
            FROM paper_ledger
            WHERE status = 'OPEN'
            ORDER BY timestamp, signal_id
        """).fetchall()
        positions = tuple(
            OpenPositionExposure(
                instrument_id=str(instrument),
                sector=str(sector or self.risk_metadata.profile(instrument).sector),
                risk_inr=float(risk_amount or abs(entry - stop) * qty),
                notional_inr=float(entry_notional or entry * qty),
            )
            for instrument, sector, risk_amount, entry_notional, entry, stop, qty in rows
        )

        now_ist = now_utc.astimezone(IST)
        start_ist = datetime.combine(now_ist.date(), clock_time.min, tzinfo=IST)
        end_ist = datetime.combine(now_ist.date(), clock_time.max, tzinfo=IST)
        start_utc = start_ist.astimezone(timezone.utc)
        end_utc = end_ist.astimezone(timezone.utc)
        daily_net = conn.execute("""
            SELECT COALESCE(SUM(net_pnl), 0.0)
            FROM paper_ledger
            WHERE status = 'CLOSED'
              AND exit_timestamp >= ? AND exit_timestamp <= ?
        """, (start_utc, end_utc)).fetchone()[0]
        return PortfolioState(positions=positions, daily_net_pnl_inr=float(daily_net))

    def evaluate_open_positions(self, current_prices: dict, current_ist_time: datetime):
        force_exit = (
            (current_ist_time.hour == 15 and current_ist_time.minute >= 15)
            or current_ist_time.hour > 15
        )

        for sig_id, pos in list(self.active_positions.items()):
            curr = current_prices.get(pos["instrument"])
            if curr is None or curr <= 0:
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
                gross_pnl = (
                    (curr - pos["entry"]) * pos["qty"]
                    if pos["direction"] == Direction.LONG
                    else (pos["entry"] - curr) * pos["qty"]
                )
                friction = self.cost_model.estimate_trade_friction(
                    entry_price=Decimal(str(pos["entry"])),
                    exit_price=Decimal(str(curr)),
                    quantity=pos["qty"],
                    slippage_bps_per_side=pos["slippage_bps_per_side"],
                )
                net_pnl = gross_pnl - float(friction.total)
                exit_timestamp = datetime.now(timezone.utc)
                hold_duration_sec = max(
                    0.0, (exit_timestamp - pos["opened_at"]).total_seconds()
                )
                self._close_position_transaction(
                    signal_id=sig_id,
                    instrument_id=pos["instrument"],
                    exit_price=curr,
                    gross_pnl=gross_pnl,
                    modeled_costs=float(friction.statutory_and_brokerage),
                    modeled_slippage=float(friction.slippage),
                    net_pnl=net_pnl,
                    exit_reason=exit_reason,
                    exit_timestamp=exit_timestamp,
                    hold_duration_sec=hold_duration_sec,
                )

                del self.active_positions[sig_id]
                print(
                    f"[POSITION-CLOSED] {pos['instrument']} | Reason: {exit_reason} "
                    f"| Exit: ₹{curr:.2f} | Gross: ₹{gross_pnl:.2f} "
                    f"| Costs: ₹{float(friction.total):.2f} | Net: ₹{net_pnl:.2f}"
                )

    def _close_position_transaction(self, **values) -> None:
        for attempt in range(self.transaction_retries):
            conn = self._get_conn()
            try:
                conn.execute("BEGIN TRANSACTION")
                conn.execute("""
                    UPDATE portfolio_admission_mutex
                    SET version = version + 1
                    WHERE mutex_id = 1
                """)
                row = conn.execute(
                    "SELECT status FROM paper_ledger WHERE signal_id = ?",
                    (values["signal_id"],),
                ).fetchone()
                if not row or row[0] != "OPEN":
                    raise RuntimeError("position is not OPEN in durable ledger")
                conn.execute("""
                    UPDATE paper_ledger
                    SET status = 'CLOSED', exit_price = ?, pnl = ?,
                        gross_pnl = ?, modeled_costs = ?, modeled_slippage = ?,
                        friction_total = ?, net_pnl = ?, exit_reason = ?,
                        exit_timestamp = ?, hold_duration_sec = ?
                    WHERE signal_id = ? AND status = 'OPEN'
                """, (
                    values["exit_price"],
                    values["gross_pnl"],
                    values["gross_pnl"],
                    values["modeled_costs"],
                    values["modeled_slippage"],
                    values["modeled_costs"] + values["modeled_slippage"],
                    values["net_pnl"],
                    values["exit_reason"],
                    values["exit_timestamp"],
                    values["hold_duration_sec"],
                    values["signal_id"],
                ))
                conn.execute("""
                    DELETE FROM open_instrument_locks
                    WHERE instrument_id = ? AND signal_id = ?
                """, (values["instrument_id"], values["signal_id"]))
                conn.execute("COMMIT")
                return
            except duckdb.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                if not _is_retryable_db_contention(exc):
                    raise
                if attempt + 1 >= self.transaction_retries:
                    raise
                time.sleep(min(0.001 * (2 ** attempt), 0.05))
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()


def _is_retryable_db_contention(exc: duckdb.Error) -> bool:
    """Retry only known optimistic-concurrency/file-attach contention errors."""
    if isinstance(exc, duckdb.TransactionException):
        return True
    message = str(exc).lower()
    return isinstance(exc, duckdb.BinderException) and (
        "unique file handle conflict" in message
        and "already attached" in message
    )
