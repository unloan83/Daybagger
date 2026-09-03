from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from daybagger.data.upstox import UpstoxDataError, UpstoxMarketData, UpstoxQuoteSnapshot


NSE_BOD_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
NSE_MIS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE_MIS.json.gz"


class UniverseError(RuntimeError):
    """Official instrument universe could not be built or validated."""


@dataclass(frozen=True, slots=True)
class EquityInstrument:
    instrument_key: str
    trading_symbol: str
    name: str
    isin: str
    tick_size: Decimal
    security_type: str | None
    cas_eligible: bool | None


@dataclass(frozen=True, slots=True)
class ObservableEquity:
    instrument: EquityInstrument
    quote: UpstoxQuoteSnapshot
    spread_bps: float | None
    session_turnover_inr: Decimal


JsonListTransport = Callable[[str, float], list[dict[str, Any]]]


class NSEEquityUniverse:
    """
    Reuses Upstox's official daily instrument files.

    Base universe:
      NSE BOD equity instruments
      INTERSECT
      NSE MIS-eligible instruments

    No home-grown symbol list and no strategy filtering live here.
    """

    def __init__(
        self,
        *,
        transport: JsonListTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self._transport = transport or _download_gz_json
        self._timeout = timeout_seconds

    def load_mis_equities(self) -> list[EquityInstrument]:
        bod = self._transport(NSE_BOD_URL, self._timeout)
        mis = self._transport(NSE_MIS_URL, self._timeout)

        if not bod:
            raise UniverseError("NSE BOD instrument file is empty")
        if not mis:
            raise UniverseError("NSE MIS instrument file is empty")

        bod_by_key: dict[str, Mapping[str, Any]] = {}
        for row in bod:
            if _is_nse_cash_equity(row):
                key = str(row.get("instrument_key") or "").strip()
                if key:
                    bod_by_key[key] = row

        mis_keys = {
            str(row.get("instrument_key") or "").strip()
            for row in mis
            if _is_nse_cash_equity(row)
        }
        mis_keys.discard("")

        common_keys = sorted(set(bod_by_key).intersection(mis_keys))
        if not common_keys:
            raise UniverseError(
                "No NSE cash equities found in the intersection of BOD and MIS files"
            )

        result = [_to_equity_instrument(bod_by_key[key]) for key in common_keys]

        symbols = [item.trading_symbol for item in result]
        if len(symbols) != len(set(symbols)):
            raise UniverseError("duplicate NSE trading symbols detected in official universe")

        return result

    def observe(
        self,
        *,
        market_data: UpstoxMarketData,
        instruments: Sequence[EquityInstrument],
        batch_size: int = 500,
    ) -> list[ObservableEquity]:
        """
        Adds real current quote information to the official universe.

        This is an observability check, not a trading strategy:
        - missing quote => fail closed for that instrument
        - no fabricated bid/ask
        - no arbitrary momentum/indicator thresholds
        """
        if not instruments:
            raise UniverseError("instruments cannot be empty")
        if not 1 <= batch_size <= 500:
            raise UniverseError("batch_size must be between 1 and 500")

        by_key = {item.instrument_key: item for item in instruments}
        observed: list[ObservableEquity] = []

        for keys in _chunks(list(by_key), batch_size):
            try:
                snapshots = market_data.full_quotes(keys)
            except UpstoxDataError as exc:
                raise UniverseError(f"quote batch failed: {exc}") from exc

            for key in keys:
                snap = snapshots.get(key)
                if snap is None:
                    raise UniverseError(f"missing quote for official instrument {key}")

                spread_bps = _spread_bps(snap)
                turnover = snap.last_price * Decimal(snap.session_volume)

                observed.append(
                    ObservableEquity(
                        instrument=by_key[key],
                        quote=snap,
                        spread_bps=spread_bps,
                        session_turnover_inr=turnover,
                    )
                )

        return observed


def usable_for_execution(item: ObservableEquity) -> bool:
    """
    Minimal data-quality gate only.

    This deliberately does NOT impose a strategy/liquidity threshold yet.
    It only checks that the stock has traded and has a real two-sided quote.
    """
    return (
        item.quote.session_volume > 0
        and item.quote.best_bid is not None
        and item.quote.best_ask is not None
        and item.quote.best_bid > 0
        and item.quote.best_ask >= item.quote.best_bid
    )


def _is_nse_cash_equity(row: Mapping[str, Any]) -> bool:
    return (
        str(row.get("segment") or "").upper() == "NSE_EQ"
        and str(row.get("exchange") or "").upper() == "NSE"
        and str(row.get("instrument_type") or "").upper() == "EQ"
    )


def _to_equity_instrument(row: Mapping[str, Any]) -> EquityInstrument:
    key = str(row.get("instrument_key") or "").strip()
    symbol = str(row.get("trading_symbol") or "").strip()
    name = str(row.get("name") or "").strip()
    isin = str(row.get("isin") or "").strip()

    if not key or not symbol or not isin:
        raise UniverseError(f"incomplete NSE equity instrument row: {row!r}")

    tick_raw = row.get("tick_size")
    try:
        tick = Decimal(str(tick_raw))
    except Exception as exc:
        raise UniverseError(f"{symbol}: invalid tick_size={tick_raw!r}") from exc
    if tick <= 0:
        raise UniverseError(f"{symbol}: tick_size must be positive")

    security = row.get("security_type")
    cas = row.get("cas_eligible")

    return EquityInstrument(
        instrument_key=key,
        trading_symbol=symbol,
        name=name,
        isin=isin,
        tick_size=tick,
        security_type=str(security) if security is not None else None,
        cas_eligible=bool(cas) if cas is not None else None,
    )


def _spread_bps(snapshot: UpstoxQuoteSnapshot) -> float | None:
    if snapshot.best_bid is None or snapshot.best_ask is None:
        return None
    mid = (snapshot.best_bid + snapshot.best_ask) / Decimal("2")
    if mid <= 0:
        return None
    return float((snapshot.best_ask - snapshot.best_bid) / mid * Decimal("10000"))


def _chunks(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _download_gz_json(url: str, timeout_seconds: float) -> list[dict[str, Any]]:
    request = Request(
        url,
        headers={"Accept": "application/json, application/gzip"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            compressed = response.read()
    except HTTPError as exc:
        raise UniverseError(f"instrument download HTTP {exc.code}: {url}") from exc
    except URLError as exc:
        raise UniverseError(f"instrument download failed: {exc.reason}") from exc

    try:
        raw = gzip.decompress(compressed)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UniverseError(f"invalid gzip/JSON instrument file: {url}") from exc

    if not isinstance(payload, list):
        raise UniverseError(f"instrument file is not a JSON list: {url}")

    return [row for row in payload if isinstance(row, dict)]
