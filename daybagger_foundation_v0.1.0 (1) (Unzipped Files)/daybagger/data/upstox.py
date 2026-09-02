from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from daybagger.domain import ExecutableQuote


class UpstoxDataError(RuntimeError):
    """Upstox data could not be retrieved or validated."""


@dataclass(frozen=True, slots=True)
class UpstoxQuoteSnapshot:
    instrument_key: str
    symbol: str
    as_of: datetime
    last_price: Decimal
    session_open: Decimal
    session_high: Decimal
    session_low: Decimal
    session_close: Decimal
    session_volume: int
    average_price: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    total_buy_quantity: int | None
    total_sell_quantity: int | None

    def to_executable_quote(self) -> ExecutableQuote:
        if self.best_bid is None or self.best_ask is None:
            raise UpstoxDataError(
                f"{self.symbol}: executable bid/ask unavailable; refusing synthetic quote."
            )
        quote_obj = ExecutableQuote(
            symbol=self.symbol,
            as_of=self.as_of,
            bid=self.best_bid,
            ask=self.best_ask,
            last=self.last_price,
        )
        quote_obj.validate()
        return quote_obj


@dataclass(frozen=True, slots=True)
class IntradayCandle:
    instrument_key: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int

    def validate(self) -> None:
        if self.timestamp.tzinfo is None:
            raise UpstoxDataError("candle timestamp must be timezone-aware")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise UpstoxDataError("OHLC prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise UpstoxDataError("candle high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise UpstoxDataError("candle low is inconsistent")
        if self.volume < 0 or self.open_interest < 0:
            raise UpstoxDataError("volume/open_interest cannot be negative")


JsonTransport = Callable[[str, Mapping[str, str], float], dict[str, Any]]


class UpstoxMarketData:
    """
    Minimal, fail-closed Upstox market-data client.

    Important semantic rule:
    - Full Market Quote OHLC/volume are SESSION SNAPSHOT fields.
    - They are NEVER converted into minute candles.
    - Minute candles come only from the Upstox V3 intraday-candle endpoint.
    """

    QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
    INTRADAY_URL = "https://api.upstox.com/v3/historical-candle/intraday"

    def __init__(
        self,
        access_token: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        transport: JsonTransport | None = None,
    ) -> None:
        token = (access_token or os.getenv("UPSTOX_ACCESS_TOKEN", "")).strip()
        if not token:
            raise UpstoxDataError(
                "UPSTOX_ACCESS_TOKEN is required. No anonymous/synthetic fallback is allowed."
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport or _default_json_transport

    def full_quotes(self, instrument_keys: Sequence[str]) -> dict[str, UpstoxQuoteSnapshot]:
        keys = _clean_keys(instrument_keys)
        if not keys:
            raise UpstoxDataError("at least one instrument key is required")
        if len(keys) > 500:
            raise UpstoxDataError("Full Market Quote accepts at most 500 instrument keys per call")

        query = urlencode({"instrument_key": ",".join(keys)})
        payload = self._get_json(f"{self.QUOTE_URL}?{query}")

        if payload.get("status") != "success":
            raise UpstoxDataError(f"Upstox quote response not successful: {payload!r}")

        raw_data = payload.get("data")
        if not isinstance(raw_data, dict) or not raw_data:
            raise UpstoxDataError("Upstox quote response contains no data")

        snapshots: dict[str, UpstoxQuoteSnapshot] = {}
        for raw in raw_data.values():
            if not isinstance(raw, dict):
                continue
            snap = _parse_quote_snapshot(raw)
            snapshots[snap.instrument_key] = snap

        missing = [key for key in keys if key not in snapshots]
        if missing:
            raise UpstoxDataError(
                "Upstox did not return all requested instruments: " + ", ".join(missing)
            )
        return snapshots

    def intraday_candles(
        self,
        instrument_key: str,
        *,
        interval_minutes: int = 1,
    ) -> list[IntradayCandle]:
        key = instrument_key.strip()
        if not key:
            raise UpstoxDataError("instrument_key is required")
        if not 1 <= interval_minutes <= 300:
            raise UpstoxDataError("interval_minutes must be between 1 and 300")

        encoded_key = quote(key, safe="")
        url = (
            f"{self.INTRADAY_URL}/{encoded_key}/minutes/{interval_minutes}"
        )
        payload = self._get_json(url)

        if payload.get("status") != "success":
            raise UpstoxDataError(f"Upstox candle response not successful: {payload!r}")

        data = payload.get("data")
        raw_candles = data.get("candles") if isinstance(data, dict) else None
        if not isinstance(raw_candles, list) or not raw_candles:
            raise UpstoxDataError(
                f"{key}: no genuine intraday candles returned; refusing fallback."
            )

        candles = [_parse_candle(key, row) for row in raw_candles]
        candles.sort(key=lambda candle: candle.timestamp)

        # Fail closed on duplicate timestamps: duplicate bars would corrupt indicators later.
        timestamps = [c.timestamp for c in candles]
        if len(timestamps) != len(set(timestamps)):
            raise UpstoxDataError(f"{key}: duplicate intraday candle timestamps detected")

        return candles

    def _get_json(self, url: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        try:
            payload = self._transport(url, headers, self._timeout)
        except UpstoxDataError:
            raise
        except Exception as exc:
            raise UpstoxDataError(f"Upstox request failed: {exc}") from exc

        if not isinstance(payload, dict):
            raise UpstoxDataError("Upstox response is not a JSON object")
        return payload


def _default_json_transport(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise UpstoxDataError(f"Upstox HTTP {exc.code}: {body[:500]}") from exc
    except URLError as exc:
        raise UpstoxDataError(f"Upstox network error: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise UpstoxDataError("Upstox returned invalid JSON") from exc


def _clean_keys(instrument_keys: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in instrument_keys:
        key = str(item).strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _parse_quote_snapshot(raw: Mapping[str, Any]) -> UpstoxQuoteSnapshot:
    instrument_key = str(raw.get("instrument_token") or "").strip()
    symbol = str(raw.get("symbol") or "").strip()
    if not instrument_key or not symbol:
        raise UpstoxDataError("quote is missing instrument_token or symbol")

    ohlc = raw.get("ohlc")
    if not isinstance(ohlc, dict):
        raise UpstoxDataError(f"{symbol}: session OHLC missing")

    as_of = _parse_timestamp(raw.get("timestamp"))

    depth = raw.get("depth") if isinstance(raw.get("depth"), dict) else {}
    best_bid = _first_positive_depth_price(depth.get("buy"))
    best_ask = _first_positive_depth_price(depth.get("sell"))

    snapshot = UpstoxQuoteSnapshot(
        instrument_key=instrument_key,
        symbol=symbol,
        as_of=as_of,
        last_price=_positive_decimal(raw.get("last_price"), f"{symbol}.last_price"),
        session_open=_positive_decimal(ohlc.get("open"), f"{symbol}.session_open"),
        session_high=_positive_decimal(ohlc.get("high"), f"{symbol}.session_high"),
        session_low=_positive_decimal(ohlc.get("low"), f"{symbol}.session_low"),
        session_close=_positive_decimal(ohlc.get("close"), f"{symbol}.session_close"),
        session_volume=_non_negative_int(raw.get("volume"), f"{symbol}.session_volume"),
        average_price=_optional_positive_decimal(raw.get("average_price")),
        best_bid=best_bid,
        best_ask=best_ask,
        total_buy_quantity=_optional_non_negative_int(raw.get("total_buy_quantity")),
        total_sell_quantity=_optional_non_negative_int(raw.get("total_sell_quantity")),
    )

    if snapshot.session_high < max(
        snapshot.session_open, snapshot.session_low, snapshot.session_close
    ):
        raise UpstoxDataError(f"{symbol}: inconsistent session high")
    if snapshot.session_low > min(
        snapshot.session_open, snapshot.session_high, snapshot.session_close
    ):
        raise UpstoxDataError(f"{symbol}: inconsistent session low")
    return snapshot


def _parse_candle(instrument_key: str, row: Any) -> IntradayCandle:
    if not isinstance(row, (list, tuple)) or len(row) < 7:
        raise UpstoxDataError(f"{instrument_key}: malformed candle: {row!r}")

    candle = IntradayCandle(
        instrument_key=instrument_key,
        timestamp=_parse_timestamp(row[0]),
        open=_positive_decimal(row[1], "candle.open"),
        high=_positive_decimal(row[2], "candle.high"),
        low=_positive_decimal(row[3], "candle.low"),
        close=_positive_decimal(row[4], "candle.close"),
        volume=_non_negative_int(row[5], "candle.volume"),
        open_interest=_non_negative_int(row[6], "candle.open_interest"),
    )
    candle.validate()
    return candle


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        # Upstox documentation/comments have used epoch milliseconds in some places.
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds).astimezone()

    text = str(value or "").strip()
    if not text:
        raise UpstoxDataError("timestamp is missing")

    if text.isdigit():
        return _parse_timestamp(int(text))

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpstoxDataError(f"invalid timestamp: {text}") from exc
    if parsed.tzinfo is None:
        raise UpstoxDataError(f"timestamp is not timezone-aware: {text}")
    return parsed


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise UpstoxDataError(f"{field} is not numeric") from exc
    if result <= 0:
        raise UpstoxDataError(f"{field} must be positive")
    return result


def _optional_positive_decimal(value: Any) -> Decimal | None:
    if value in (None, "", 0, 0.0, "0"):
        return None
    result = Decimal(str(value))
    return result if result > 0 else None


def _non_negative_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except Exception as exc:
        raise UpstoxDataError(f"{field} is not an integer") from exc
    if result < 0:
        raise UpstoxDataError(f"{field} cannot be negative")
    return result


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    result = int(value)
    return result if result >= 0 else None


def _first_positive_depth_price(levels: Any) -> Decimal | None:
    if not isinstance(levels, list):
        return None
    for level in levels:
        if not isinstance(level, dict):
            continue
        value = level.get("price")
        try:
            price = Decimal(str(value))
        except Exception:
            continue
        if price > 0:
            return price
    return None
