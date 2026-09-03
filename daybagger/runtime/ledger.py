from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from daybagger.domain import Direction, ExecutableQuote


class LedgerError(RuntimeError):
    """Paper ledger operation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class Position:
    position_id: str
    symbol: str
    direction: Direction
    quantity: int
    entry_price: Decimal
    opened_at: datetime
    instrument_key: str | None = None
    opportunity_id: str | None = None
    validation_id: str | None = None
    reserved_capital_inr: Decimal | None = None
    max_loss_inr: Decimal | None = None
    stop_price: Decimal | None = None
    horizon_minutes: int | None = None


class PaperLedger:
    """
    Single authoritative paper ledger.

    Legacy ``open`` remains for smoke tests. Production paper execution must use
    ``open_fill`` so the ledger records the PaperBroker's actual fill (including
    configured paper slippage), not the pre-execution quote.
    """

    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_price TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    instrument_key TEXT,
                    opportunity_id TEXT,
                    validation_id TEXT,
                    reserved_capital_inr TEXT,
                    max_loss_inr TEXT,
                    stop_price TEXT,
                    horizon_minutes INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_price TEXT NOT NULL,
                    exit_price TEXT NOT NULL,
                    gross_pnl_inr TEXT NOT NULL,
                    costs_inr TEXT,
                    net_pnl_inr TEXT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    exit_reason TEXT
                )
                """
            )
            _ensure_columns(conn, "positions", {
                "instrument_key": "TEXT",
                "opportunity_id": "TEXT",
                "validation_id": "TEXT",
                "reserved_capital_inr": "TEXT",
                "max_loss_inr": "TEXT",
                "stop_price": "TEXT",
                "horizon_minutes": "INTEGER",
            })
            _ensure_columns(conn, "trades", {"exit_reason": "TEXT"})
            conn.commit()

    def open(
        self,
        *,
        symbol: str,
        direction: Direction,
        quantity: int,
        quote: ExecutableQuote,
        now: datetime,
    ) -> Position:
        """Legacy quote-side paper open used by foundation tests."""
        quote.validate()
        if symbol != quote.symbol:
            raise LedgerError("symbol/quote mismatch")
        if direction == Direction.LONG:
            price = quote.ask
        elif direction == Direction.SHORT:
            price = quote.bid
        else:
            raise LedgerError("FLAT cannot open a position")
        return self.open_fill(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            filled_price=price,
            now=now,
        )

    def open_fill(
        self,
        *,
        symbol: str,
        direction: Direction,
        quantity: int,
        filled_price: Decimal,
        now: datetime,
        instrument_key: str | None = None,
        opportunity_id: str | None = None,
        validation_id: str | None = None,
        reserved_capital_inr: Decimal | None = None,
        max_loss_inr: Decimal | None = None,
        stop_price: Decimal | None = None,
        horizon_minutes: int | None = None,
    ) -> Position:
        if now.tzinfo is None:
            raise LedgerError("now must be timezone-aware")
        if not symbol.strip():
            raise LedgerError("symbol is required")
        if direction == Direction.FLAT:
            raise LedgerError("FLAT cannot open a position")
        if quantity <= 0 or filled_price <= 0:
            raise LedgerError("quantity/filled_price must be positive")
        if reserved_capital_inr is not None and reserved_capital_inr <= 0:
            raise LedgerError("reserved_capital_inr must be positive")
        if max_loss_inr is not None and max_loss_inr <= 0:
            raise LedgerError("max_loss_inr must be positive")
        if stop_price is not None and stop_price <= 0:
            raise LedgerError("stop_price must be positive")
        if horizon_minutes is not None and horizon_minutes <= 0:
            raise LedgerError("horizon_minutes must be positive")

        position = Position(
            position_id=str(uuid4()),
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            entry_price=filled_price,
            opened_at=now,
            instrument_key=instrument_key,
            opportunity_id=opportunity_id,
            validation_id=validation_id,
            reserved_capital_inr=reserved_capital_inr,
            max_loss_inr=max_loss_inr,
            stop_price=stop_price,
            horizon_minutes=horizon_minutes,
        )
        with sqlite3.connect(self.path) as conn:
            existing = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE symbol=? AND status='OPEN'",
                (symbol,),
            ).fetchone()[0]
            if existing:
                raise LedgerError(f"{symbol}: open position already exists")
            conn.execute(
                """
                INSERT INTO positions(
                    position_id, symbol, direction, quantity, entry_price, opened_at,
                    status, instrument_key, opportunity_id, validation_id,
                    reserved_capital_inr, max_loss_inr, stop_price, horizon_minutes
                ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.position_id,
                    position.symbol,
                    position.direction.value,
                    position.quantity,
                    str(position.entry_price),
                    position.opened_at.isoformat(),
                    position.instrument_key,
                    position.opportunity_id,
                    position.validation_id,
                    _dec(position.reserved_capital_inr),
                    _dec(position.max_loss_inr),
                    _dec(position.stop_price),
                    position.horizon_minutes,
                ),
            )
            conn.commit()
        return position

    def close(
        self,
        *,
        position_id: str,
        quote: ExecutableQuote,
        now: datetime,
        costs_inr: Decimal | None = None,
        exit_reason: str | None = None,
    ) -> Decimal:
        quote.validate()
        if now.tzinfo is None:
            raise LedgerError("now must be timezone-aware")
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT symbol, direction FROM positions WHERE position_id=?",
                (position_id,),
            ).fetchone()
        if not row:
            raise LedgerError("position not found")
        symbol, direction_raw = row
        if quote.symbol != symbol:
            raise LedgerError("symbol/quote mismatch")
        direction = Direction(direction_raw)
        exit_price = quote.bid if direction == Direction.LONG else quote.ask
        return self.close_fill(
            position_id=position_id,
            filled_price=exit_price,
            now=now,
            costs_inr=costs_inr,
            exit_reason=exit_reason,
        )

    def close_fill(
        self,
        *,
        position_id: str,
        filled_price: Decimal,
        now: datetime,
        costs_inr: Decimal | None = None,
        exit_reason: str | None = None,
    ) -> Decimal:
        if now.tzinfo is None:
            raise LedgerError("now must be timezone-aware")
        if filled_price <= 0:
            raise LedgerError("filled_price must be positive")
        if costs_inr is not None and costs_inr < 0:
            raise LedgerError("costs_inr cannot be negative")

        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                """
                SELECT symbol, direction, quantity, entry_price, opened_at, status
                FROM positions WHERE position_id=?
                """,
                (position_id,),
            ).fetchone()
            if not row:
                raise LedgerError("position not found")
            symbol, direction_raw, qty, entry_raw, opened_at, status = row
            if status != "OPEN":
                raise LedgerError("position is already closed")

            direction = Direction(direction_raw)
            entry = Decimal(entry_raw)
            gross = (
                (filled_price - entry) * Decimal(qty)
                if direction == Direction.LONG
                else (entry - filled_price) * Decimal(qty)
            )
            net = gross - costs_inr if costs_inr is not None else None
            trade_id = str(uuid4())
            conn.execute(
                "UPDATE positions SET status='CLOSED' WHERE position_id=?",
                (position_id,),
            )
            conn.execute(
                """
                INSERT INTO trades(
                    trade_id, position_id, symbol, direction, quantity, entry_price,
                    exit_price, gross_pnl_inr, costs_inr, net_pnl_inr, opened_at,
                    closed_at, exit_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id, position_id, symbol, direction.value, qty, str(entry),
                    str(filled_price), str(gross),
                    str(costs_inr) if costs_inr is not None else None,
                    str(net) if net is not None else None, opened_at, now.isoformat(),
                    exit_reason,
                ),
            )
            conn.commit()
        return gross

    def open_positions(self) -> list[Position]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                """
                SELECT position_id, symbol, direction, quantity, entry_price, opened_at,
                       instrument_key, opportunity_id, validation_id,
                       reserved_capital_inr, max_loss_inr, stop_price, horizon_minutes
                FROM positions WHERE status='OPEN' ORDER BY opened_at
                """
            ).fetchall()
        return [_position_from_row(row) for row in rows]

    def open_capital_inr(self) -> Decimal:
        total = Decimal("0")
        for pos in self.open_positions():
            total += pos.reserved_capital_inr or pos.entry_price * Decimal(pos.quantity)
        return total

    def open_risk_inr(self) -> Decimal:
        return sum(
            (pos.max_loss_inr or Decimal("0") for pos in self.open_positions()),
            Decimal("0"),
        )

    def realised_pnl(self) -> tuple[Decimal, Decimal | None]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT gross_pnl_inr, costs_inr, net_pnl_inr FROM trades"
            ).fetchall()
        return _sum_pnl_rows(rows)

    def equity_and_peak(self, starting_equity_inr: Decimal) -> tuple[Decimal, Decimal]:
        if starting_equity_inr <= 0:
            raise LedgerError("starting_equity_inr must be positive")
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT net_pnl_inr FROM trades ORDER BY closed_at, rowid"
            ).fetchall()
        equity = starting_equity_inr
        peak = starting_equity_inr
        for (net_raw,) in rows:
            if net_raw is None:
                raise LedgerError("cannot compute equity from trade with missing net P&L")
            equity += Decimal(str(net_raw))
            peak = max(peak, equity)
        return equity, peak

    def realised_pnl_for_date(
        self,
        trading_date: date,
        *,
        timezone_name: str = "Asia/Kolkata",
    ) -> tuple[Decimal, Decimal | None]:
        tz = ZoneInfo(timezone_name)
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT gross_pnl_inr, costs_inr, net_pnl_inr, closed_at FROM trades"
            ).fetchall()
        selected = []
        for gross, costs, net, closed_raw in rows:
            closed = datetime.fromisoformat(str(closed_raw))
            if closed.tzinfo is None:
                continue
            if closed.astimezone(tz).date() == trading_date:
                selected.append((gross, costs, net))
        return _sum_pnl_rows(selected)


def _position_from_row(row) -> Position:
    return Position(
        position_id=str(row[0]),
        symbol=str(row[1]),
        direction=Direction(str(row[2])),
        quantity=int(row[3]),
        entry_price=Decimal(str(row[4])),
        opened_at=datetime.fromisoformat(str(row[5])),
        instrument_key=str(row[6]) if row[6] else None,
        opportunity_id=str(row[7]) if row[7] else None,
        validation_id=str(row[8]) if row[8] else None,
        reserved_capital_inr=Decimal(str(row[9])) if row[9] else None,
        max_loss_inr=Decimal(str(row[10])) if row[10] else None,
        stop_price=Decimal(str(row[11])) if row[11] else None,
        horizon_minutes=int(row[12]) if row[12] is not None else None,
    )


def _sum_pnl_rows(rows) -> tuple[Decimal, Decimal | None]:
    gross = sum((Decimal(str(row[0])) for row in rows), Decimal("0"))
    if any(row[1] is None or row[2] is None for row in rows):
        return gross, None
    net = sum((Decimal(str(row[2])) for row in rows), Decimal("0"))
    return gross, net


def _ensure_columns(conn: sqlite3.Connection, table: str, additions: dict[str, str]) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _dec(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
