from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from daybagger.data.universe import EquityInstrument, ObservableEquity
from daybagger.data.upstox import IntradayCandle, UpstoxQuoteSnapshot
from daybagger.intelligence.engine import (
    breadth_features,
    microstructure_features,
    relative_strength_features,
    sector_strength_features,
    time_normalized_volume_features,
)
from daybagger.intelligence.market_context import ContextFeatures


NOW = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)


def snap(symbol, last, open_, volume, bid, ask, buy=1000, sell=900):
    return UpstoxQuoteSnapshot(
        instrument_key=f"NSE_EQ|{symbol}",
        symbol=symbol,
        as_of=NOW,
        last_price=Decimal(str(last)),
        session_open=Decimal(str(open_)),
        session_high=Decimal(str(max(last, open_) + 1)),
        session_low=Decimal(str(min(last, open_) - 1)),
        session_close=Decimal(str(last)),
        session_volume=volume,
        average_price=None,
        best_bid=Decimal(str(bid)) if bid is not None else None,
        best_ask=Decimal(str(ask)) if ask is not None else None,
        total_buy_quantity=buy,
        total_sell_quantity=sell,
    )


def observable(snapshot):
    instrument = EquityInstrument(
        instrument_key=snapshot.instrument_key,
        trading_symbol=snapshot.symbol,
        name=snapshot.symbol,
        isin=f"ISIN-{snapshot.symbol}",
        tick_size=Decimal("0.05"),
        security_type="NORMAL",
        cas_eligible=True,
    )
    spread = None
    if snapshot.best_bid is not None and snapshot.best_ask is not None:
        mid = (snapshot.best_bid + snapshot.best_ask) / Decimal("2")
        spread = float((snapshot.best_ask - snapshot.best_bid) / mid * Decimal("10000"))
    return ObservableEquity(
        instrument=instrument,
        quote=snapshot,
        spread_bps=spread,
        session_turnover_inr=snapshot.last_price * Decimal(snapshot.session_volume),
    )


def context(key, ret, ret15, eff):
    return ContextFeatures(
        instrument_key=key,
        bars=30,
        session_return_bps=ret,
        return_5m_bps=ret / 3,
        return_15m_bps=ret15,
        return_30m_bps=ret,
        session_range_bps=abs(ret) + 50,
        trend_efficiency=eff,
        close_location=0.7,
    )


def candle(key, minute, volume, close=100):
    return IntradayCandle(
        instrument_key=key,
        timestamp=NOW + timedelta(minutes=minute),
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=volume,
        open_interest=0,
    )


def test_breadth_uses_real_cross_section() -> None:
    observed = [
        observable(snap("AAA", 102, 100, 1000, 101.9, 102.1)),
        observable(snap("BBB", 99, 100, 2000, 98.9, 99.1)),
        observable(snap("CCC", 100, 100, 1500, None, None)),
    ]
    b = breadth_features(observed)
    assert b.observed_stocks == 3
    assert b.advances == 1
    assert b.declines == 1
    assert b.unchanged == 1
    assert b.advance_ratio == pytest.approx(1 / 3)
    assert b.pct_with_two_sided_quote == pytest.approx(2 / 3)


def test_microstructure_never_invents_depth() -> None:
    s = snap("AAA", 100, 99, 1000, None, None, buy=1200, sell=800)
    m = microstructure_features(s)
    assert m.spread_bps is None
    assert m.buy_sell_quantity_imbalance == pytest.approx(0.2)


def test_relative_strength_is_excess_return_not_score() -> None:
    s = snap("AAA", 101, 100, 1000, 100.9, 101.1)
    benchmark = context("NIFTY", 40, 20, 0.5)
    sector = context("IT", 70, 30, 0.6)
    rs = relative_strength_features(snapshot=s, benchmark=benchmark, sector=sector)
    assert rs.stock_session_return_bps == pytest.approx(100.0)
    assert rs.versus_benchmark_bps == pytest.approx(60.0)
    assert rs.versus_sector_bps == pytest.approx(30.0)


def test_time_normalized_volume_compares_same_elapsed_bars() -> None:
    key = "NSE_EQ|AAA"
    current = [candle(key, i, 200) for i in range(5)]  # 1000 total
    history = [
        [candle(key, i, 100) for i in range(10)],      # first 5 => 500
        [candle(key, i, 150) for i in range(10)],      # first 5 => 750
        [candle(key, i, 50) for i in range(3)],        # ignored, too short
    ]
    v = time_normalized_volume_features(
        symbol="AAA",
        current_session=current,
        historical_sessions=history,
    )
    assert v.comparable_sessions == 2
    assert v.historical_median_cumulative_volume == pytest.approx(625.0)
    assert v.relative_volume == pytest.approx(1.6)


def test_sector_strength_has_separate_percentiles_not_composite_score() -> None:
    contexts = {
        "IT": context("IT", 120, 60, 0.8),
        "BANK": context("BANK", 20, 10, 0.4),
        "METAL": context("METAL", -50, -20, 0.3),
    }
    ranked = sector_strength_features(contexts)
    assert ranked[0].sector_key == "IT"
    assert ranked[0].session_return_percentile > ranked[-1].session_return_percentile
    assert not hasattr(ranked[0], "score")
