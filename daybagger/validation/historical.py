from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Sequence
from urllib.parse import quote

from daybagger.data.upstox import (
    IntradayCandle,
    UpstoxDataError,
    UpstoxMarketData,
    parse_candle,
)


class HistoricalCandleClient:
    """
    Thin reuse layer over the existing Upstox client.

    Upstox V3 historical minute data is available from January 2022.
    For 1-15 minute intervals, requests are kept within one-month chunks.
    """

    BASE_URL = "https://api.upstox.com/v3/historical-candle"

    def __init__(
        self,
        market_data: UpstoxMarketData,
        *,
        cache_dir: Path | None = None,
    ):
        self.market_data = market_data
        self.cache_dir = cache_dir

    def fetch(
        self,
        instrument_key: str,
        *,
        from_date: date,
        to_date: date,
        interval_minutes: int = 1,
    ) -> list[IntradayCandle]:
        key = instrument_key.strip()
        if not key:
            raise UpstoxDataError("instrument_key is required")
        if from_date > to_date:
            raise UpstoxDataError("from_date cannot be after to_date")
        if not 1 <= interval_minutes <= 300:
            raise UpstoxDataError("interval_minutes must be between 1 and 300")

        cache_path = self._cache_path(
            key,
            from_date=from_date,
            to_date=to_date,
            interval_minutes=interval_minutes,
        )
        if cache_path is not None and cache_path.exists():
            try:
                return _read_cached_candles(cache_path, key)
            except (OSError, ValueError, KeyError, TypeError, UpstoxDataError):
                cache_path.unlink(missing_ok=True)

        rows: list[IntradayCandle] = []
        for chunk_from, chunk_to in _monthly_chunks(from_date, to_date):
            encoded = quote(key, safe="")
            url = (
                f"{self.BASE_URL}/{encoded}/minutes/{interval_minutes}/"
                f"{chunk_to.isoformat()}/{chunk_from.isoformat()}"
            )
            payload = self.market_data.request_json(url)
            if payload.get("status") != "success":
                raise UpstoxDataError(
                    f"historical candle response not successful: {payload!r}"
                )
            data = payload.get("data")
            raw = data.get("candles") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise UpstoxDataError(f"{key}: historical candles missing")
            rows.extend(parse_candle(key, item) for item in raw)

        rows.sort(key=lambda c: c.timestamp)
        deduped: dict = {c.timestamp: c for c in rows}
        if len(deduped) != len(rows):
            # Exact duplicate timestamps across chunk boundaries are harmless;
            # conflicting duplicate content is not.
            for candle in rows:
                other = deduped[candle.timestamp]
                if candle != other:
                    raise UpstoxDataError(
                        f"{key}: conflicting duplicate historical candle"
                    )
        result = list(sorted(deduped.values(), key=lambda c: c.timestamp))
        if cache_path is not None:
            _write_cached_candles(cache_path, result)
        return result

    def _cache_path(
        self,
        instrument_key: str,
        *,
        from_date: date,
        to_date: date,
        interval_minutes: int,
    ) -> Path | None:
        if self.cache_dir is None:
            return None
        identity = f"{instrument_key}|{from_date}|{to_date}|{interval_minutes}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{digest}.json.gz"


def _monthly_chunks(start: date, end: date):
    """
    Inclusive chunks, each no longer than 28 calendar days.
    This stays safely inside Upstox's one-month retrieval window for 1-15m bars.
    """
    from datetime import timedelta

    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=27), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _read_cached_candles(path: Path, instrument_key: str) -> list[IntradayCandle]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema") != "daybagger-historical-candles-v1":
        raise ValueError("unsupported historical cache schema")
    rows = payload.get("candles")
    if not isinstance(rows, list) or not rows:
        raise ValueError("historical cache contains no candles")
    candles = [
        parse_candle(instrument_key, row)
        for row in rows
    ]
    timestamps = [c.timestamp for c in candles]
    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise UpstoxDataError("historical cache candle timestamps are invalid")
    return candles


def _write_cached_candles(path: Path, candles: Sequence[IntradayCandle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "daybagger-historical-candles-v1",
        "source": "Upstox historical candle API",
        "candles": [
            [
                candle.timestamp.isoformat(),
                str(candle.open),
                str(candle.high),
                str(candle.low),
                str(candle.close),
                candle.volume,
                candle.open_interest,
            ]
            for candle in candles
        ],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    temporary.replace(path)
