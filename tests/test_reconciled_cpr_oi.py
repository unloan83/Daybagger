from datetime import date, datetime, timedelta
from pathlib import Path

from daybagger.validation.reconciled_cpr_oi import (
    Bar, DailyBar, ReconciledDataPipeline, Trade, clustered_bootstrap_ci,
)


def daily(day, symbol="ABC", instrument="1", expiry=None, volume=10, oi=100, lot=5):
    return DailyBar(day, symbol, instrument, "INE000A01001", expiry, 100, 105, 95, 102, volume, oi, lot)


def test_rolls_at_start_of_expiry_day_and_tags_cohort(tmp_path):
    day = date(2026, 8, 25)
    pipeline = ReconciledDataPipeline("token", tmp_path)
    selected = pipeline.select_contracts({day: {"ABC": [
        daily(day, instrument="near", expiry=day),
        daily(day, instrument="next", expiry=date(2026, 9, 29)),
    ]}})
    assert selected[day]["ABC"].bar.instrument_id == "next"
    assert selected[day]["ABC"].cohort == "ROLLOVER_EXPIRY"


def test_futures_reconciliation_applies_historical_lot_size(tmp_path):
    day = date(2026, 8, 24)
    pipeline = ReconciledDataPipeline("token", tmp_path)
    expected = daily(day, volume=2, oi=100, lot=5)
    bars = [Bar(datetime.fromisoformat("2026-08-24T09:15:00+05:30"), 100, 105, 95, 102, 10, 100)]
    assert pipeline.reconcile(expected, bars, futures=True)


def test_reconciliation_excludes_mismatch(tmp_path):
    day = date(2026, 8, 24)
    pipeline = ReconciledDataPipeline("token", tmp_path)
    bars = [Bar(datetime.fromisoformat("2026-08-24T09:15:00+05:30"), 100, 105, 95, 103, 10, 100)]
    assert not pipeline.reconcile(daily(day, volume=2), bars, futures=True)
    assert pipeline.exclusions[-1]["reason"] == "OHLC_MISMATCH"


def test_day_clustered_bootstrap_is_seeded():
    start = datetime.fromisoformat("2026-08-24T09:15:00+05:30")
    trades = []
    for offset, pnl in enumerate((10.0, -5.0, 20.0)):
        opened = start + timedelta(days=offset)
        trades.append(Trade(opened.date(), "ABC", "STANDARD", opened, opened + timedelta(minutes=5), "LONG", 1, 100, 101, pnl, pnl, 1))
    assert clustered_bootstrap_ci(trades, seed=7) == clustered_bootstrap_ci(trades, seed=7)
