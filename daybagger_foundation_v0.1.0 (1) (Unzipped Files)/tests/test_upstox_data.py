from datetime import datetime
from decimal import Decimal

import pytest

from daybagger.data.upstox import UpstoxDataError, UpstoxMarketData


KEY = "NSE_EQ|INE002A01018"


def _transport(url, headers, timeout):
    if "/market-quote/quotes" in url:
        return {
            "status": "success",
            "data": {
                "NSE_EQ:RELIANCE": {
                    "ohlc": {
                        "open": 100.0,
                        "high": 105.0,
                        "low": 99.0,
                        "close": 103.0,
                    },
                    "depth": {
                        "buy": [{"quantity": 20, "price": 102.95, "orders": 2}],
                        "sell": [{"quantity": 25, "price": 103.05, "orders": 3}],
                    },
                    "timestamp": "2026-09-02T12:00:00+05:30",
                    "instrument_token": KEY,
                    "symbol": "RELIANCE",
                    "last_price": 103.0,
                    "volume": 123456,
                    "average_price": 101.5,
                    "total_buy_quantity": 1000,
                    "total_sell_quantity": 900,
                }
            },
        }

    return {
        "status": "success",
        "data": {
            # Deliberately newest-first to confirm client normalises chronological order.
            "candles": [
                ["2026-09-02T09:16:00+05:30", 101, 102, 100.5, 101.5, 1200, 0],
                ["2026-09-02T09:15:00+05:30", 100, 101, 99.5, 100.8, 1000, 0],
            ]
        },
    }


def test_quote_snapshot_is_not_a_candle() -> None:
    client = UpstoxMarketData(access_token="test", transport=_transport)
    snap = client.full_quotes([KEY])[KEY]

    assert snap.session_open == Decimal("100.0")
    assert snap.session_volume == 123456
    assert snap.best_bid == Decimal("102.95")
    assert snap.best_ask == Decimal("103.05")

    executable = snap.to_executable_quote()
    assert executable.bid == Decimal("102.95")
    assert executable.ask == Decimal("103.05")


def test_intraday_candles_are_real_separate_objects_and_sorted() -> None:
    client = UpstoxMarketData(access_token="test", transport=_transport)
    candles = client.intraday_candles(KEY, interval_minutes=1)

    assert len(candles) == 2
    assert candles[0].timestamp.minute == 15
    assert candles[1].timestamp.minute == 16
    assert candles[0].volume == 1000
    assert candles[1].volume == 1200


def test_missing_depth_never_creates_fake_bid_ask() -> None:
    def no_depth_transport(url, headers, timeout):
        payload = _transport(url, headers, timeout)
        if "/market-quote/quotes" in url:
            payload["data"]["NSE_EQ:RELIANCE"]["depth"] = {"buy": [], "sell": []}
        return payload

    client = UpstoxMarketData(access_token="test", transport=no_depth_transport)
    snap = client.full_quotes([KEY])[KEY]

    assert snap.best_bid is None
    assert snap.best_ask is None
    with pytest.raises(UpstoxDataError):
        snap.to_executable_quote()


def test_no_token_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    with pytest.raises(UpstoxDataError):
        UpstoxMarketData(access_token=None)


def test_missing_requested_instrument_fails_closed() -> None:
    def missing_transport(url, headers, timeout):
        return {"status": "success", "data": {}}

    client = UpstoxMarketData(access_token="test", transport=missing_transport)
    with pytest.raises(UpstoxDataError):
        client.full_quotes([KEY])


def test_duplicate_candle_timestamp_is_rejected() -> None:
    def duplicate_transport(url, headers, timeout):
        if "/market-quote/quotes" in url:
            return _transport(url, headers, timeout)
        return {
            "status": "success",
            "data": {
                "candles": [
                    ["2026-09-02T09:15:00+05:30", 100, 101, 99, 100.5, 1000, 0],
                    ["2026-09-02T09:15:00+05:30", 100, 101, 99, 100.5, 1000, 0],
                ]
            },
        }

    client = UpstoxMarketData(access_token="test", transport=duplicate_transport)
    with pytest.raises(UpstoxDataError):
        client.intraday_candles(KEY)
