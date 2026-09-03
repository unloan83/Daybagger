from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from daybagger.domain import Direction, ExecutableQuote
from daybagger.runtime.eod import EODReporter
from daybagger.runtime.ledger import LedgerError, PaperLedger
from daybagger.runtime.session import SessionGuard, SessionState


NOW = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)


def quote(symbol, bid, ask):
    return ExecutableQuote(
        symbol=symbol,
        as_of=NOW,
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        last=Decimal(str((bid + ask) / 2)),
    )


def test_long_and_short_pnl_are_side_aware(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "paper.sqlite3")
    ledger.initialize()

    long = ledger.open(
        symbol="AAA",
        direction=Direction.LONG,
        quantity=10,
        quote=quote("AAA", 99.9, 100.0),
        now=NOW,
    )
    long_gross = ledger.close(
        position_id=long.position_id,
        quote=quote("AAA", 101.0, 101.1),
        now=NOW,
        costs_inr=Decimal("2"),
    )
    assert long_gross == Decimal("10.0")

    short = ledger.open(
        symbol="BBB",
        direction=Direction.SHORT,
        quantity=10,
        quote=quote("BBB", 100.0, 100.1),
        now=NOW,
    )
    short_gross = ledger.close(
        position_id=short.position_id,
        quote=quote("BBB", 98.9, 99.0),
        now=NOW,
        costs_inr=Decimal("2"),
    )
    assert short_gross == Decimal("10.0")


def test_duplicate_open_position_is_blocked(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "paper.sqlite3")
    ledger.initialize()
    ledger.open(
        symbol="AAA",
        direction=Direction.LONG,
        quantity=1,
        quote=quote("AAA", 99.9, 100.0),
        now=NOW,
    )
    with pytest.raises(LedgerError):
        ledger.open(
            symbol="AAA",
            direction=Direction.LONG,
            quantity=1,
            quote=quote("AAA", 99.9, 100.0),
            now=NOW,
        )


def test_eod_net_is_unknown_when_costs_are_missing(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "paper.sqlite3")
    ledger.initialize()
    pos = ledger.open(
        symbol="AAA",
        direction=Direction.LONG,
        quantity=1,
        quote=quote("AAA", 99.9, 100.0),
        now=NOW,
    )
    ledger.close(
        position_id=pos.position_id,
        quote=quote("AAA", 101.0, 101.1),
        now=NOW,
        costs_inr=None,
    )
    summary = EODReporter(ledger).summarize()
    assert summary.costs_complete is False
    assert summary.net_realised_pnl_inr is None


def test_session_guard_has_separate_entry_cutoff_and_exit() -> None:
    guard = SessionGuard()
    india = __import__("zoneinfo").ZoneInfo("Asia/Kolkata")

    assert guard.state(datetime(2026, 9, 2, 9, 0, tzinfo=india)) == SessionState.PREMARKET
    assert guard.state(datetime(2026, 9, 2, 10, 0, tzinfo=india)) == SessionState.MARKET
    assert guard.state(datetime(2026, 9, 2, 15, 7, tzinfo=india)) == SessionState.ENTRY_CLOSED
    assert guard.state(datetime(2026, 9, 2, 15, 10, tzinfo=india)) == SessionState.CLOSED


def test_ledger_persists_actual_paper_fill_and_reservations(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "paper.sqlite3")
    ledger.initialize()
    fill = Decimal("100.07")
    pos = ledger.open_fill(
        symbol="AAA",
        direction=Direction.LONG,
        quantity=25,
        filled_price=fill,
        now=NOW,
        instrument_key="NSE_EQ|AAA",
        opportunity_id="opp-1",
        validation_id="meta-1",
        reserved_capital_inr=fill * 25,
        max_loss_inr=Decimal("250"),
        stop_price=Decimal("98"),
        horizon_minutes=30,
    )
    loaded = ledger.open_positions()[0]
    assert loaded.position_id == pos.position_id
    assert loaded.entry_price == fill
    assert loaded.reserved_capital_inr == fill * 25
    assert ledger.open_capital_inr() == fill * 25
    assert ledger.open_risk_inr() == Decimal("250")
