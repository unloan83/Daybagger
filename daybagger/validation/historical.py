from __future__ import annotations

from datetime import date
from typing import Sequence
from urllib.parse import quote

from daybagger.data.upstox import (
    IntradayCandle,
    UpstoxDataError,
    UpstoxMarketData,
    _parse_candle,
)


class HistoricalCandleClient:
    """
    Thin reuse layer over the existing Upstox client.

    Upstox V3 historical minute data is available from January 2022.
    For 1-15 minute intervals, requests are kept within one-month chunks.
    """

    BASE_URL = "https://api.upstox.com/v3/historical-candle"

    def __init__(self, market_data: UpstoxMarketData):
        self.market_data = market_data

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

        rows: list[IntradayCandle] = []
        for chunk_from, chunk_to in _monthly_chunks(from_date, to_date):
            encoded = quote(key, safe="")
            url = (
                f"{self.BASE_URL}/{encoded}/minutes/{interval_minutes}/"
                f"{chunk_to.isoformat()}/{chunk_from.isoformat()}"
            )
            payload = self.market_data._get_json(url)
            if payload.get("status") != "success":
                raise UpstoxDataError(
                    f"historical candle response not successful: {payload!r}"
                )
            data = payload.get("data")
            raw = data.get("candles") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise UpstoxDataError(f"{key}: historical candles missing")
            rows.extend(_parse_candle(key, item) for item in raw)

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
        return list(sorted(deduped.values(), key=lambda c: c.timestamp))


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
