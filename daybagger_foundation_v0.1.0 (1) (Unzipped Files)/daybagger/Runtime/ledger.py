from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from uuid import uuid4

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


class PaperLedger:
    """
    Single paper ledger.

    Entry:
      LONG -> real ask
      SHORT -> real bid
    Exit:
      LONG -> real bid
      SHORT -> real ask

    No invented exit prices and side-aware P&L.
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
                    status TEXT NOT NULL
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
                    closed_at TEXT NOT NULL
                )
                """
            )
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
        quote.validate()
        if now.tzinfo is None:
            raise LedgerError("now must be timezone-aware")
        if quantity <= 0:
            raise LedgerError("quantity must be > 0")
        if symbol != quote.symbol:
            raise LedgerError("symbol/quote mismatch")
        if direction == Direction.LONG:
            price = quote.ask
        elif direction == Direction.SHORT:
            price = quote.bid
        else:
            raise LedgerError("FLAT cannot open a position")

        position = Position(
            position_id=str(uuid4()),
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            entry_price=price,
            opened_at=now,
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
                INSERT INTO positions(position_id, symbol, direction, quantity, entry_price, opened_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN')
                """,
                (
                    position.position_id,
                    position.symbol,
                    position.direction.value,
                    position.quantity,
                    str(position.entry_price),
                    position.opened_at.isoformat(),
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
    ) -> Decimal:
        quote.validate()
        if now.tzinfo is None:
            raise LedgerError("now must be timezone-aware")
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
            if quote.symbol != symbol:
                raise LedgerError("symbol/quote mismatch")

            direction = Direction(direction_raw)
            entry = Decimal(entry_raw)
            if direction == Direction.LONG:
                exit_price = quote.bid
                gross = (exit_price - entry) * Decimal(qty)
            else:
                exit_price = quote.ask
                gross = (entry - exit_price) * Decimal(qty)

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
                    exit_price, gross_pnl_inr, costs_inr, net_pnl_inr, opened_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    position_id,
                    symbol,
                    direction.value,
                    qty,
                    str(entry),
                    str(exit_price),
                    str(gross),
                    str(costs_inr) if costs_inr is not None else None,
                    str(net) if net is not None else None,
                    opened_at,
                    now.isoformat(),
                ),
            )
            conn.commit()
        return gross

    def open_positions(self) -> list[Position]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                """
                SELECT position_id, symbol, direction, quantity, entry_price, opened_at
                FROM positions WHERE status='OPEN' ORDER BY opened_at
                """
            ).fetchall()
        return [
            Position(
                position_id=row[0],
                symbol=row[1],
                direction=Direction(row[2]),
                quantity=int(row[3]),
                entry_price=Decimal(row[4]),
                opened_at=datetime.fromisoformat(row[5]),
            )
            for row in rows
        ]

    def realised_pnl(self) -> tuple[Decimal, Decimal | None]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT gross_pnl_inr, costs_inr, net_pnl_inr FROM trades"
            ).fetchall()
        gross = sum((Decimal(row[0]) for row in rows), Decimal("0"))
        if any(row[1] is None or row[2] is None for row in rows):
            return gross, None
        net = sum((Decimal(row[2]) for row in rows), Decimal("0"))
        return gross, net
