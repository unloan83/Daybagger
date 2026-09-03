from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from daybagger.data.upstox import IntradayCandle
from daybagger.intelligence.market_context import (
    MarketContextError,
    compute_context_features,
)


KEY = "NSE_INDEX|Nifty 50"


def candle(minute, o, h, l, c):
    return IntradayCandle(
        instrument_key=KEY,
        timestamp=datetime(2026, 9, 2, 9, 15, tzinfo=timezone.utc)
        + timedelta(minutes=minute),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=0,
        open_interest=0,
    )


def test_context_features_use_observed_price_path_only() -> None:
    bars = [
        candle(0, 100, 101, 99.5, 100.5),
        candle(1, 100.5, 101.5, 100.2, 101.2),
        candle(2, 101.2, 102, 101, 101.8),
        candle(3, 101.8, 102.5, 101.5, 102.2),
        candle(4, 102.2, 103, 102, 102.8),
    ]

    features = compute_context_features(bars)

    assert features.instrument_key == KEY
    assert features.bars == 5
    assert features.session_return_bps == pytest.approx(280.0)
    assert features.return_5m_bps == pytest.approx(280.0)
    assert features.return_15m_bps is None
    assert features.session_range_bps == pytest.approx(350.0)
    assert 0.0 <= features.trend_efficiency <= 1.0
    assert 0.0 <= features.close_location <= 1.0


def test_trend_efficiency_distinguishes_clean_from_noisy_path() -> None:
    clean = [
        candle(0, 100, 101, 99.9, 101),
        candle(1, 101, 102, 100.9, 102),
        candle(2, 102, 103, 101.9, 103),
        candle(3, 103, 104, 102.9, 104),
        candle(4, 104, 105, 103.9, 105),
    ]
    noisy = [
        candle(0, 100, 102, 99.5, 101),
        candle(1, 101, 101.2, 99.8, 100),
        candle(2, 100, 103, 99.9, 102),
        candle(3, 102, 102.2, 100.5, 101),
        candle(4, 101, 105, 100.8, 105),
    ]

    clean_eff = compute_context_features(clean).trend_efficiency
    noisy_eff = compute_context_features(noisy).trend_efficiency

    assert clean_eff > noisy_eff


def test_flat_market_has_neutral_range_location() -> None:
    bars = [candle(0, 100, 100, 100, 100)]
    features = compute_context_features(bars)

    assert features.session_return_bps == 0
    assert features.session_range_bps == 0
    assert features.trend_efficiency == 0
    assert features.close_location == 0.5


def test_mixed_instruments_fail_closed() -> None:
    other = IntradayCandle(
        instrument_key="NSE_INDEX|India VIX",
        timestamp=datetime(2026, 9, 2, 9, 16, tzinfo=timezone.utc),
        open=Decimal("12"),
        high=Decimal("13"),
        low=Decimal("11"),
        close=Decimal("12.5"),
        volume=0,
        open_interest=0,
    )

    with pytest.raises(MarketContextError):
        compute_context_features([candle(0, 100, 101, 99, 100.5), other])
