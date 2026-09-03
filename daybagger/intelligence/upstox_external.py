from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from daybagger.data.upstox import UpstoxDataError, UpstoxMarketData


GLOBAL_INSTRUMENTS_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/global.json.gz"
)
INDIA = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class GlobalInstrument:
    name: str
    trading_symbol: str
    instrument_key: str
    segment: str
    latency: str | None


@dataclass(frozen=True, slots=True)
class InstitutionalDailyFeatures:
    session_date: date
    values: Mapping[str, float]


class UpstoxExternalIntelligence:
    """
    Free/official intelligence available through Upstox.

    Important timing rule: FII/DII activity is end-of-day information. Historical
    intraday decisions may only use the most recent record strictly BEFORE the
    decision session. The helper ``lagged_institutional_features`` enforces that.
    """

    FII_URL = "https://api.upstox.com/v2/market/fii"
    DII_URL = "https://api.upstox.com/v2/market/dii"
    NEWS_URL = "https://api.upstox.com/v2/news"
    PROFILE_URL = "https://api.upstox.com/v2/fundamentals/{isin}/profile"

    def __init__(self, market_data: UpstoxMarketData):
        self.market_data = market_data

    def global_instruments(self) -> list[GlobalInstrument]:
        payload = _download_gz_json(GLOBAL_INSTRUMENTS_URL)
        result: list[GlobalInstrument] = []
        for row in payload:
            segment = str(row.get("segment") or "").strip()
            if segment not in {"GLOBAL_INDEX", "GLOBAL_INDICATOR"}:
                continue
            key = str(row.get("instrument_key") or "").strip()
            name = str(row.get("name") or "").strip()
            trading_symbol = str(row.get("trading_symbol") or "").strip()
            if not key or not (name or trading_symbol):
                continue
            result.append(
                GlobalInstrument(
                    name=name,
                    trading_symbol=trading_symbol,
                    instrument_key=key,
                    segment=segment,
                    latency=(
                        str(row.get("latency")) if row.get("latency") is not None else None
                    ),
                )
            )
        if not result:
            raise UpstoxDataError("global instrument file contained no usable instruments")
        return result

    def resolve_global(self, names: Sequence[str]) -> dict[str, GlobalInstrument]:
        wanted = {name.strip().upper(): name.strip() for name in names if name.strip()}
        if not wanted:
            return {}
        resolved: dict[str, GlobalInstrument] = {}
        for item in self.global_instruments():
            aliases = {item.name.upper(), item.trading_symbol.upper()}
            for upper, original in wanted.items():
                if upper in aliases:
                    resolved[original] = item
        return resolved

    def company_sector(self, isin: str) -> str:
        clean = isin.strip()
        if not clean:
            raise ValueError("isin is required")
        payload = self.market_data.request_json(
            self.PROFILE_URL.format(isin=quote(clean, safe=""))
        )
        if payload.get("status") != "success":
            raise UpstoxDataError(f"company profile response not successful: {payload!r}")
        data = payload.get("data")
        sector = str(data.get("sector") or "").strip() if isinstance(data, dict) else ""
        if not sector:
            raise UpstoxDataError(f"{clean}: company profile did not provide sector")
        return sector

    def fii_daily(self, *, from_date: date) -> dict[date, Mapping[str, float]]:
        data_types = (
            "NSE_EQ|CASH",
            "NSE_FO|INDEX_FUTURES",
            "NSE_FO|STOCK_FUTURES",
            "NSE_FO|INDEX_OPTIONS",
            "NSE_FO|STOCK_OPTIONS",
        )
        params: list[tuple[str, str]] = [("data_type", item) for item in data_types]
        params.extend([("interval", "1D"), ("from", from_date.isoformat())])
        payload = self.market_data.request_json(f"{self.FII_URL}?{urlencode(params)}")
        return _parse_fii_payload(payload)

    def dii_daily(self, *, from_date: date) -> dict[date, Mapping[str, float]]:
        params = urlencode(
            {
                "data_type": "NSE_EQ|CASH",
                "interval": "1D",
                "from": from_date.isoformat(),
            }
        )
        payload = self.market_data.request_json(f"{self.DII_URL}?{params}")
        return _parse_dii_payload(payload)

    def institutional_history(
        self,
        *,
        from_date: date,
        to_date: date | None = None,
    ) -> dict[date, Mapping[str, float]]:
        """
        Fetch the full daily FII/DII history without silently truncating at the
        API's 30-trading-day per-request limit. Requests advance in 28-calendar-
        day steps and overlapping responses are merged by date.
        """
        end = to_date or datetime.now(INDIA).date()
        if from_date > end:
            raise ValueError("from_date cannot be after to_date")

        fii_all: dict[date, Mapping[str, float]] = {}
        dii_all: dict[date, Mapping[str, float]] = {}
        cursor = from_date
        while cursor <= end:
            fii_all.update(self.fii_daily(from_date=cursor))
            dii_all.update(self.dii_daily(from_date=cursor))
            cursor += timedelta(days=28)

        dates = sorted(
            day for day in set(fii_all).union(dii_all)
            if from_date <= day <= end
        )
        result: dict[date, Mapping[str, float]] = {}
        for day in dates:
            values: dict[str, float] = {}
            values.update(fii_all.get(day, {}))
            values.update(dii_all.get(day, {}))
            result[day] = values
        if not result:
            raise UpstoxDataError("institutional history contained no usable records")
        return result

    def news(self, instrument_keys: Sequence[str]) -> dict[str, list[Mapping[str, Any]]]:
        keys = [str(key).strip() for key in instrument_keys if str(key).strip()]
        if not keys:
            return {}
        if len(keys) > 30:
            raise ValueError("news endpoint supports at most 30 instrument keys per request")
        params = urlencode(
            {
                "category": "instrument_keys",
                "instrument_keys": ",".join(keys),
                "page_number": 1,
                "page_size": 100,
            }
        )
        payload = self.market_data.request_json(f"{self.NEWS_URL}?{params}")
        if payload.get("status") != "success":
            raise UpstoxDataError(f"news response not successful: {payload!r}")
        raw = payload.get("data")
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): [item for item in value if isinstance(item, dict)]
            for key, value in raw.items()
            if isinstance(value, list)
        }


def lagged_institutional_features(
    history: Mapping[date, Mapping[str, float]],
    session_date: date,
) -> Mapping[str, float] | None:
    """Return the latest EOD institutional record strictly before session_date."""
    eligible = [day for day in history if day < session_date]
    if not eligible:
        return None
    return history[max(eligible)]


def load_sector_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("sector cache must contain a JSON object")
    return {str(k): str(v) for k, v in payload.items() if str(k) and str(v)}


def save_sector_cache(path: Path, mapping: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(sorted(mapping.items())), indent=2), encoding="utf-8")
    tmp.replace(path)


def _parse_fii_payload(payload: Mapping[str, Any]) -> dict[date, Mapping[str, float]]:
    if payload.get("status") != "success":
        raise UpstoxDataError(f"FII response not successful: {payload!r}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise UpstoxDataError("FII response data missing")

    by_date: dict[date, dict[str, float]] = {}
    aliases = {
        "NSE_EQ|CASH": "fii_cash",
        "NSE_FO|INDEX_FUTURES": "fii_index_futures",
        "NSE_FO|STOCK_FUTURES": "fii_stock_futures",
        "NSE_FO|INDEX_OPTIONS": "fii_index_options",
        "NSE_FO|STOCK_OPTIONS": "fii_stock_options",
    }
    for data_type, prefix in aliases.items():
        rows = data.get(data_type)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = _record_date(row.get("time_stamp"))
            target = by_date.setdefault(day, {})
            buy = _number(row.get("buy_amount"))
            sell = _number(row.get("sell_amount"))
            target[f"{prefix}_net_amount_ratio"] = _signed_ratio(buy - sell, abs(buy) + abs(sell))

            long_contracts = _number(row.get("total_long_contracts"))
            short_contracts = _number(row.get("total_short_contracts"))
            if long_contracts + short_contracts > 0:
                target[f"{prefix}_position_ratio"] = _signed_ratio(
                    long_contracts - short_contracts,
                    long_contracts + short_contracts,
                )

            option_fields = {
                "call_long_share": _number(row.get("total_call_long_contracts")),
                "put_long_share": _number(row.get("total_put_long_contracts")),
                "call_short_share": _number(row.get("total_call_short_contracts")),
                "put_short_share": _number(row.get("total_put_short_contracts")),
            }
            total_options = sum(option_fields.values())
            if total_options > 0:
                for suffix, value in option_fields.items():
                    target[f"{prefix}_{suffix}"] = value / total_options
    if not by_date:
        raise UpstoxDataError("FII response contained no usable daily records")
    return by_date


def _parse_dii_payload(payload: Mapping[str, Any]) -> dict[date, Mapping[str, float]]:
    if payload.get("status") != "success":
        raise UpstoxDataError(f"DII response not successful: {payload!r}")
    data = payload.get("data")
    rows = data.get("NSE_EQ|CASH") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise UpstoxDataError("DII cash response missing")
    result: dict[date, Mapping[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        buy = _number(row.get("buy_amount"))
        sell = _number(row.get("sell_amount"))
        result[_record_date(row.get("time_stamp"))] = {
            "dii_cash_net_amount_ratio": _signed_ratio(
                buy - sell,
                abs(buy) + abs(sell),
            )
        }
    if not result:
        raise UpstoxDataError("DII response contained no usable daily records")
    return result


def _record_date(raw: Any) -> date:
    try:
        value = float(raw)
    except Exception as exc:
        raise UpstoxDataError(f"invalid institutional timestamp: {raw!r}") from exc
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).astimezone(INDIA).date()


def _number(raw: Any) -> float:
    if raw in (None, ""):
        return 0.0
    try:
        return float(raw)
    except Exception as exc:
        raise UpstoxDataError(f"invalid numeric external value: {raw!r}") from exc


def _signed_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    value = numerator / denominator
    return max(-1.0, min(1.0, float(value)))


def _download_gz_json(url: str, timeout_seconds: float = 15.0) -> list[dict[str, Any]]:
    request = Request(
        url,
        headers={
            "Accept": "application/json, application/gzip",
            "User-Agent": "Mozilla/5.0 Daybagger/0.1",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            compressed = response.read()
    except HTTPError as exc:
        raise UpstoxDataError(f"global instrument download HTTP {exc.code}") from exc
    except URLError as exc:
        raise UpstoxDataError(f"global instrument download failed: {exc.reason}") from exc

    try:
        payload = json.loads(gzip.decompress(compressed).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstoxDataError("invalid global instrument gzip/JSON") from exc
    if not isinstance(payload, list):
        raise UpstoxDataError("global instrument file is not a JSON list")
    return [row for row in payload if isinstance(row, dict)]
