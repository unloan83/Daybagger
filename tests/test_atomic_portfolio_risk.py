from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from daybagger.engine.order_desk import PaperOrderDesk
from daybagger.engine.risk_gate import (
    OpenPositionExposure,
    PortfolioRiskGate,
    PortfolioRiskLimits,
    PortfolioState,
    RiskDesk,
)
from daybagger.engine.risk_metadata import InstrumentRiskMetadata
from trading_contracts.schemas.v1 import Direction, MarketRegime, SignalCandidate


def _signal(signal_id: str, symbol: str, *, entry: float = 100.0) -> SignalCandidate:
    now = datetime.now(timezone.utc)
    return SignalCandidate(
        signal_id=signal_id,
        instrument_id=symbol,
        created_at=now,
        valid_until=now,
        direction=Direction.LONG,
        setup_type="CPR_OI_INTELLIGENCE",
        regime=MarketRegime.TRENDING,
        confidence=0.9,
        entry_trigger=entry,
        invalidation_level=entry - 10.0,
        reason_codes=["TEST"],
    )


def _metadata(symbols: list[str], *, correlations=None) -> InstrumentRiskMetadata:
    profiles = {
        symbol: (f"INE{index:09d}", f"SECTOR-{index}")
        for index, symbol in enumerate(symbols, start=1)
    }
    return InstrumentRiskMetadata.from_profiles(
        profiles, correlations=correlations or {}
    )


def test_atomic_duplicate_constraint_with_simultaneous_writers_repeatedly():
    """Thirty independent races must each commit exactly one OPEN instrument."""
    risk = RiskDesk()
    with TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "atomic.duckdb")
        symbols = [f"RACE{round_no:02d}" for round_no in range(30)]
        metadata = _metadata(symbols)
        bootstrap = PaperOrderDesk(db_path=db_path, risk_metadata=metadata)

        for round_no, symbol in enumerate(symbols):
            desks = [
                PaperOrderDesk(db_path=db_path, risk_metadata=metadata)
                for _ in range(8)
            ]
            signals = [_signal(f"race-{round_no}-{i}", symbol) for i in range(8)]
            evaluations = [risk.evaluate(signal) for signal in signals]
            barrier = threading.Barrier(8)
            results: list[str] = []
            errors: list[BaseException] = []

            def writer(index: int) -> None:
                try:
                    barrier.wait()
                    results.append(desks[index].record_signal(signals[index], evaluations[index]))
                except BaseException as exc:  # captured for assertion in main thread
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            assert not errors
            assert all(not thread.is_alive() for thread in threads)
            assert results.count("OPEN") == 1
            assert results.count("DUPLICATE_DB_LOCKED") == 7
            with duckdb.connect(db_path, read_only=True) as conn:
                assert conn.execute(
                    "SELECT count(*) FROM paper_ledger WHERE instrument_id=? AND status='OPEN'",
                    (symbol,),
                ).fetchone()[0] == 1
                assert conn.execute(
                    "SELECT count(*) FROM open_instrument_locks WHERE instrument_id=?",
                    (symbol,),
                ).fetchone()[0] == 1

            winner = next(desk for desk in desks if desk.active_positions)
            winning_id = next(iter(winner.active_positions))
            winner.evaluate_open_positions(
                {symbol: winner.active_positions[winning_id]["target"]},
                datetime.now(timezone.utc),
            )


def test_atomic_portfolio_cap_with_simultaneous_distinct_instruments_repeatedly():
    """Serialized admissions cannot race past the aggregate-risk limit."""
    risk = RiskDesk()
    symbols = [f"CAP{i}" for i in range(8)]
    correlations = {
        (left, right): 0.0
        for index, left in enumerate(symbols)
        for right in symbols[index + 1:]
    }
    metadata = _metadata(symbols, correlations=correlations)
    for round_no in range(10):
        with TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "cap.duckdb")
            desks = [
                PaperOrderDesk(db_path=db_path, risk_metadata=metadata)
                for _ in symbols
            ]
            signals = [
                _signal(f"cap-{round_no}-{index}", symbol)
                for index, symbol in enumerate(symbols)
            ]
            evaluations = [risk.evaluate(signal) for signal in signals]
            barrier = threading.Barrier(len(symbols))
            results: list[str] = []
            errors: list[BaseException] = []

            def writer(index: int) -> None:
                try:
                    barrier.wait()
                    results.append(desks[index].record_signal(signals[index], evaluations[index]))
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            assert not errors
            assert results.count("OPEN") == 2
            assert results.count("REJECTED") == 6
            with duckdb.connect(db_path, read_only=True) as conn:
                assert conn.execute(
                    "SELECT count(*) FROM paper_ledger WHERE status='OPEN'"
                ).fetchone()[0] == 2
                assert conn.execute(
                    "SELECT COALESCE(sum(risk_amount),0) FROM paper_ledger WHERE status='OPEN'"
                ).fetchone()[0] <= 1000.0


def test_portfolio_gate_enforces_aggregate_risk_and_gross_notional():
    symbols = ["AAA", "BBB", "CCC"]
    metadata = _metadata(symbols, correlations={
        ("AAA", "BBB"): 0.10,
        ("AAA", "CCC"): 0.10,
        ("BBB", "CCC"): 0.10,
    })
    risk = RiskDesk()
    limits = PortfolioRiskLimits(
        max_open_positions=4,
        max_aggregate_open_risk_inr=750.0,
        max_gross_notional_inr=100000.0,
        hard_daily_loss_limit_inr=1000.0,
        max_sector_risk_inr=750.0,
        max_sector_notional_inr=100000.0,
        max_correlated_risk_inr=750.0,
        max_correlated_positions=2,
    )
    with TemporaryDirectory() as tmp_dir:
        desk = PaperOrderDesk(
            db_path=str(Path(tmp_dir) / "portfolio.duckdb"),
            risk_metadata=metadata,
            portfolio_gate=PortfolioRiskGate(limits),
        )
        first = _signal("first", "AAA")
        second = _signal("second", "BBB")
        assert desk.record_signal(first, risk.evaluate(first)) == "OPEN"
        assert desk.record_signal(second, risk.evaluate(second)) == "REJECTED"
        with desk._get_conn() as conn:
            assert conn.execute(
                "SELECT rejection_reason FROM paper_ledger WHERE signal_id='second'"
            ).fetchone()[0] == "AGGREGATE_OPEN_RISK_LIMIT_REACHED"


def test_sector_and_empirical_correlation_caps_are_enforced():
    profiles = {
        "AAA": ("INE000000001", "IT - Software"),
        "BBB": ("INE000000002", "IT - Software"),
        "CCC": ("INE000000003", "Bank"),
    }
    metadata = InstrumentRiskMetadata.from_profiles(profiles, correlations={
        ("AAA", "BBB"): 0.20,
        ("AAA", "CCC"): 0.90,
        ("BBB", "CCC"): 0.10,
    })
    risk = RiskDesk()
    with TemporaryDirectory() as tmp_dir:
        desk = PaperOrderDesk(
            db_path=str(Path(tmp_dir) / "concentration.duckdb"),
            risk_metadata=metadata,
        )
        first = _signal("first", "AAA")
        same_sector = _signal("same-sector", "BBB")
        correlated = _signal("correlated", "CCC")
        assert desk.record_signal(first, risk.evaluate(first)) == "OPEN"
        assert desk.record_signal(same_sector, risk.evaluate(same_sector)) == "REJECTED"
        assert desk.record_signal(correlated, risk.evaluate(correlated)) == "REJECTED"
        with desk._get_conn() as conn:
            reasons = dict(conn.execute("""
                SELECT signal_id, rejection_reason FROM paper_ledger
                WHERE signal_id IN ('same-sector', 'correlated')
            """).fetchall())
        assert reasons == {
            "same-sector": "SECTOR_RISK_LIMIT_REACHED",
            "correlated": "CORRELATED_RISK_LIMIT_REACHED",
        }


def test_missing_sector_or_pair_correlation_fails_closed():
    risk = RiskDesk()
    metadata = _metadata(["AAA", "BBB"])
    with TemporaryDirectory() as tmp_dir:
        desk = PaperOrderDesk(
            db_path=str(Path(tmp_dir) / "failclosed.duckdb"),
            risk_metadata=metadata,
        )
        first = _signal("first", "AAA")
        second = _signal("second", "BBB")
        unknown = _signal("unknown", "UNKNOWN")
        assert desk.record_signal(first, risk.evaluate(first)) == "OPEN"
        assert desk.record_signal(second, risk.evaluate(second)) == "REJECTED"
        assert desk.record_signal(unknown, risk.evaluate(unknown)) == "REJECTED"
        with desk._get_conn() as conn:
            reasons = dict(conn.execute("""
                SELECT signal_id, rejection_reason FROM paper_ledger
                WHERE signal_id IN ('second', 'unknown')
            """).fetchall())
        assert reasons == {
            "second": "CORRELATION_METADATA_MISSING",
            "unknown": "RISK_METADATA_MISSING",
        }


def test_every_portfolio_limit_has_a_deterministic_rejection():
    limits = PortfolioRiskLimits()
    gate = PortfolioRiskGate(limits)
    base = dict(
        instrument_id="NEW",
        sector="NEW-SECTOR",
        candidate_risk_inr=500.0,
        candidate_notional_inr=50000.0,
    )
    assert gate.rejection_reason(
        **base,
        state=PortfolioState(positions=(), daily_net_pnl_inr=-1000.0),
        correlations={},
    ) == "DAILY_LOSS_LIMIT_REACHED"

    four = tuple(
        OpenPositionExposure(f"P{i}", f"S{i}", 100.0, 10000.0)
        for i in range(4)
    )
    assert gate.rejection_reason(
        **base,
        state=PortfolioState(positions=four, daily_net_pnl_inr=0.0),
        correlations={},
    ) == "MAX_OPEN_POSITIONS_REACHED"

    aggregate = (OpenPositionExposure("P", "OTHER", 600.0, 10000.0),)
    assert gate.rejection_reason(
        **base,
        state=PortfolioState(positions=aggregate, daily_net_pnl_inr=0.0),
        correlations={"P": 0.0},
    ) == "AGGREGATE_OPEN_RISK_LIMIT_REACHED"

    notional = (OpenPositionExposure("P", "OTHER", 100.0, 160000.0),)
    assert gate.rejection_reason(
        **base,
        state=PortfolioState(positions=notional, daily_net_pnl_inr=0.0),
        correlations={"P": 0.0},
    ) == "GROSS_NOTIONAL_LIMIT_REACHED"

    sector_risk = (OpenPositionExposure("P", "NEW-SECTOR", 300.0, 10000.0),)
    assert gate.rejection_reason(
        **base,
        state=PortfolioState(positions=sector_risk, daily_net_pnl_inr=0.0),
        correlations={"P": 0.0},
    ) == "SECTOR_RISK_LIMIT_REACHED"

    sector_notional = (
        OpenPositionExposure("P", "NEW-SECTOR", 100.0, 60000.0),
    )
    assert gate.rejection_reason(
        **base,
        state=PortfolioState(positions=sector_notional, daily_net_pnl_inr=0.0),
        correlations={"P": 0.0},
    ) == "SECTOR_NOTIONAL_LIMIT_REACHED"

    correlated_risk = (OpenPositionExposure("P", "OTHER", 300.0, 10000.0),)
    assert gate.rejection_reason(
        **base,
        state=PortfolioState(positions=correlated_risk, daily_net_pnl_inr=0.0),
        correlations={"P": 0.9},
    ) == "CORRELATED_RISK_LIMIT_REACHED"

    two_correlated = (
        OpenPositionExposure("P1", "OTHER1", 100.0, 10000.0),
        OpenPositionExposure("P2", "OTHER2", 100.0, 10000.0),
    )
    assert gate.rejection_reason(
        **base,
        state=PortfolioState(positions=two_correlated, daily_net_pnl_inr=0.0),
        correlations={"P1": 0.8, "P2": -0.8},
    ) == "CORRELATED_POSITION_COUNT_LIMIT_REACHED"
