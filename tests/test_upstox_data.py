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


def test_market_timings_and_exchange_status_use_official_payloads() -> None:
    calls = []
    def transport(url, headers, timeout):
        calls.append(url)
        if "/market/timings/" in url:
            return {
                "status": "success",
                "data": [{
                    "exchange": "NSE",
                    "start_time": 1788407100000,
                    "end_time": 1788429600000,
                }],
            }
        if "/market/status/" in url:
            return {"status": "success", "data": {"exchange": "NSE", "status": "NORMAL_OPEN"}}
        raise AssertionError(url)

    client = UpstoxMarketData(access_token="test", transport=transport)
    timing = client.market_timings(__import__("datetime").date(2026, 9, 3))[0]
    assert timing.exchange == "NSE"
    assert timing.start_time.tzinfo is not None
    assert timing.end_time > timing.start_time
    assert client.exchange_status("NSE") == "NORMAL_OPEN"
    assert any("/market/timings/2026-09-03" in url for url in calls)


def test_transient_upstox_errors_retry_but_auth_errors_do_not(monkeypatch) -> None:
    from daybagger.data.upstox import UpstoxTransientDataError

    transient_calls = {"n": 0}
    def transient(url, headers, timeout):
        transient_calls["n"] += 1
        if transient_calls["n"] < 3:
            raise UpstoxTransientDataError("temporary")
        return {"status": "success", "data": {}}

    client = UpstoxMarketData(
        access_token="test", transport=transient, max_attempts=3, retry_backoff_seconds=0,
    )
    assert client.request_json("https://example.test") == {"status": "success", "data": {}}
    assert transient_calls["n"] == 3

    auth_calls = {"n": 0}
    def auth_error(url, headers, timeout):
        auth_calls["n"] += 1
        raise UpstoxDataError("401 invalid token")

    client2 = UpstoxMarketData(
        access_token="test", transport=auth_error, max_attempts=3, retry_backoff_seconds=0,
    )
    with pytest.raises(UpstoxDataError):
        client2.request_json("https://example.test")
    assert auth_calls["n"] == 1
