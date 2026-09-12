from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import random
import statistics
import threading
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from daybagger.integration.costs import IndiaEquityIntradayCostModel
from trading_contracts.execution import adjust_price_for_corporate_actions

IST = ZoneInfo("Asia/Kolkata")
NSE_FO = "https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{stamp}_F_0000.csv.zip"
NSE_CM = "https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{stamp}_F_0000.csv.zip"
UPSTOX_V3 = "https://api.upstox.com/v3/historical-candle"
UPSTOX_EXPIRED = "https://api.upstox.com/v2/expired-instruments/historical-candle"
UPSTOX_ACTIONS = "https://api.upstox.com/v2/fundamentals/{isin}/corporate-actions"


@dataclass(frozen=True, slots=True)
class DailyBar:
    session_date: date
    symbol: str
    instrument_id: str
    isin: str
    expiry: date | None
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int
    lot_size: int


@dataclass(frozen=True, slots=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int


@dataclass(frozen=True, slots=True)
class SelectedContract:
    bar: DailyBar
    cohort: str


@dataclass(frozen=True, slots=True)
class Trade:
    session_date: date
    symbol: str
    cohort: str
    opened_at: datetime
    closed_at: datetime
    direction: str
    quantity: int
    entry: float
    exit: float
    net_pnl: float
    net_expectancy: float
    risk_inr: float


class DataIntegrityError(RuntimeError):
    pass


class DatasetHasher:
    def __init__(self) -> None:
        self._items: dict[str, str] = {}
        self._lock = threading.Lock()

    def add(self, identity: str, payload: bytes) -> None:
        digest = hashlib.sha256(payload).hexdigest()
        with self._lock:
            previous = self._items.get(identity)
            if previous is not None and previous != digest:
                raise DataIntegrityError(f"dataset identity changed during run: {identity}")
            self._items[identity] = digest

    @property
    def hexdigest(self) -> str:
        with self._lock:
            return canonical_json_hash(self._items)


class ReconciledDataPipeline:
    """Fail-closed Upstox/NSE historical data builder; it never fills gaps."""

    def __init__(self, token: str, cache_dir: Path, *, timeout: float = 45.0):
        if not token.strip():
            raise DataIntegrityError("UPSTOX_ACCESS_TOKEN is required")
        self.token = token.strip()
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.hasher = DatasetHasher()
        self.exclusions: list[dict[str, str]] = []
        self._api_lock = threading.Lock()
        self._last_api_call = 0.0

    def nse_sessions(
        self, start: date, end: date
    ) -> tuple[dict[date, dict[str, DailyBar]], dict[date, dict[str, list[DailyBar]]]]:
        cash: dict[date, dict[str, DailyBar]] = {}
        futures: dict[date, dict[str, list[DailyBar]]] = {}
        weekdays = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                weekdays.append(cursor)
            cursor += timedelta(days=1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = {
                session: (
                    pool.submit(self._download_nse, session, "cm"),
                    pool.submit(self._download_nse, session, "fo"),
                )
                for session in weekdays
            }
            for session in weekdays:
                cm_payload, fo_payload = (job.result() for job in jobs[session])
                if (cm_payload is None) != (fo_payload is None):
                    raise DataIntegrityError(f"partial NSE archive for {session}")
                if cm_payload is not None:
                    future_rows = self._parse_futures(session, fo_payload or b"")
                    futures[session] = future_rows
                    cash[session] = self._parse_cash(
                        session, cm_payload, allowed_symbols=set(future_rows)
                    )
        if not cash or set(cash) != set(futures):
            raise DataIntegrityError("NSE session archive is empty or inconsistent")
        return cash, futures

    def select_contracts(
        self, futures: Mapping[date, Mapping[str, Sequence[DailyBar]]]
    ) -> dict[date, dict[str, SelectedContract]]:
        selected: dict[date, dict[str, SelectedContract]] = {}
        for session, symbols in futures.items():
            selected[session] = {}
            for symbol, contracts in symbols.items():
                ordered = sorted((x for x in contracts if x.expiry and x.expiry >= session), key=lambda x: (x.expiry, x.instrument_id))
                if not ordered:
                    continue
                cohort = "STANDARD"
                choice = ordered[0]
                if choice.expiry == session:
                    if len(ordered) < 2:
                        self._exclude(session, symbol, "ROLLOVER_NEXT_MONTH_MISSING")
                        continue
                    choice = ordered[1]
                    cohort = "ROLLOVER_EXPIRY"
                selected[session][symbol] = SelectedContract(choice, cohort)
        return selected

    def candles(self, key: str, start: date, end: date, *, expired: bool) -> list[Bar]:
        identity = f"{'expired' if expired else 'active'}|{key}|{start}|{end}|5minute"
        path = self.cache_dir / "upstox" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        if path.exists():
            payload = path.read_bytes()
        else:
            encoded = urllib.parse.quote(key, safe="")
            if expired:
                url = f"{UPSTOX_EXPIRED}/{encoded}/5minute/{end}/{start}"
            else:
                url = f"{UPSTOX_V3}/{encoded}/minutes/5/{end}/{start}"
            payload = self._request(url, authenticated=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.hasher.add(identity, payload)
        raw = json.loads(payload)
        rows = raw.get("data", {}).get("candles")
        if not isinstance(rows, list):
            raise DataIntegrityError(f"Upstox candles missing: {identity}")
        parsed = [self._bar(row) for row in rows]
        parsed.sort(key=lambda x: x.timestamp)
        if len({x.timestamp for x in parsed}) != len(parsed):
            raise DataIntegrityError(f"duplicate Upstox timestamps: {identity}")
        return parsed

    def corporate_actions(self, isin: str) -> list[dict[str, Any]]:
        identity = f"corporate-actions|{isin}"
        path = self.cache_dir / "actions" / f"{isin}.json"
        if path.exists():
            payload = path.read_bytes()
        else:
            payload = self._request(UPSTOX_ACTIONS.format(isin=isin), authenticated=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.hasher.add(identity, payload)
        raw = json.loads(payload)
        return raw.get("data") if isinstance(raw.get("data"), list) else []

    def reconcile(
        self, expected: DailyBar, bars: Sequence[Bar], *, futures: bool
    ) -> bool:
        day = [x for x in bars if x.timestamp.astimezone(IST).date() == expected.session_date]
        if not day:
            self._exclude(expected.session_date, expected.symbol, "UPSTOX_DAY_MISSING")
            return False
        aggregate = (day[0].open, max(x.high for x in day), min(x.low for x in day), day[-1].close)
        references = (expected.open, expected.high, expected.low, expected.close)
        price_ok = all(abs(a - b) <= max(0.051, abs(b) * 0.00001) for a, b in zip(aggregate, references))
        expected_volume = expected.volume * expected.lot_size if futures else expected.volume
        volume_ok = sum(x.volume for x in day) == expected_volume
        oi_ok = (not futures) or day[-1].oi == expected.oi
        if not (price_ok and volume_ok and oi_ok):
            reasons = []
            if not price_ok:
                reasons.append("OHLC_MISMATCH")
            if not volume_ok:
                reasons.append("VOLUME_MISMATCH")
            if not oi_ok:
                reasons.append("OI_MISMATCH")
            self._exclude(expected.session_date, expected.symbol, "+".join(reasons))
            return False
        return True

    def adjusted(self, value: float, on_date: date, actions: Sequence[Mapping[str, Any]], end: date) -> float:
        try:
            return adjust_price_for_corporate_actions(value, on_date, actions, end)
        except ValueError as exc:
            raise DataIntegrityError(str(exc)) from exc

    def _download_nse(self, session: date, segment: str) -> bytes | None:
        identity = f"nse-{segment}|{session}"
        path = self.cache_dir / "nse" / segment / f"{session:%Y%m%d}.zip"
        if path.exists():
            payload = path.read_bytes()
        else:
            url = (NSE_CM if segment == "cm" else NSE_FO).format(stamp=f"{session:%Y%m%d}")
            try:
                payload = self._request(url, authenticated=False)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                raise
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.hasher.add(identity, payload)
        return payload

    def _request(self, url: str, *, authenticated: bool) -> bytes:
        headers = {"Accept": "application/json,application/zip", "User-Agent": "DualEngine-Shadow/1.0"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        lock = self._api_lock if authenticated else _NullLock()
        with lock:
            for attempt in range(8):
                if authenticated:
                    pause = 1.25 - (time_module.monotonic() - self._last_api_call)
                    if pause > 0:
                        time_module.sleep(pause)
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        if authenticated:
                            self._last_api_call = time_module.monotonic()
                        return response.read()
                except urllib.error.HTTPError as exc:
                    if authenticated:
                        self._last_api_call = time_module.monotonic()
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == 7:
                        raise
                    retry_after = exc.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else (
                        20.0 if exc.code == 429 else 0.5 * (2 ** attempt)
                    )
                    time_module.sleep(min(delay, 60.0))
                except (urllib.error.URLError, TimeoutError):
                    if authenticated:
                        self._last_api_call = time_module.monotonic()
                    if attempt == 7:
                        raise
                    time_module.sleep(min(0.5 * (2 ** attempt), 60.0))
        raise AssertionError("unreachable")

    @staticmethod
    def _csv(payload: bytes) -> Iterable[dict[str, str]]:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise DataIntegrityError("NSE archive must contain exactly one file")
            with archive.open(names[0]) as raw:
                yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))

    def _parse_cash(
        self, session: date, payload: bytes, *, allowed_symbols: set[str]
    ) -> dict[str, DailyBar]:
        out = {}
        for row in self._selected_rows(payload, "STK"):
            if row["FinInstrmTp"] != "STK" or row["SctySrs"] != "EQ" or not row["ISIN"]:
                continue
            bar = _daily(row, session, expiry=None)
            if bar.symbol in allowed_symbols:
                out[bar.symbol] = bar
        return out

    def _parse_futures(self, session: date, payload: bytes) -> dict[str, list[DailyBar]]:
        out: dict[str, list[DailyBar]] = defaultdict(list)
        for row in self._selected_rows(payload, "STF"):
            expiry = date.fromisoformat(row["XpryDt"])
            out[row["TckrSymb"]].append(_daily(row, session, expiry=expiry))
        return dict(out)

    @staticmethod
    def _selected_rows(payload: bytes, instrument_type: str) -> Iterable[dict[str, str]]:
        """Filter large UDiFF archives before constructing row dictionaries."""
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise DataIntegrityError("NSE archive must contain exactly one file")
            with archive.open(names[0]) as raw:
                header = next(csv.reader([raw.readline().decode("utf-8-sig")]))
                type_index = header.index("FinInstrmTp")
                if type_index != 4:
                    raise DataIntegrityError("unexpected UDiFF instrument type position")
                wanted = instrument_type.encode("ascii")
                for line in raw:
                    prefix = line.split(b",", 5)
                    if len(prefix) < 6 or prefix[4] != wanted:
                        continue
                    values = next(csv.reader([line.decode("utf-8")]))
                    if len(values) == len(header):
                        yield dict(zip(header, values))

    @staticmethod
    def _bar(row: Sequence[Any]) -> Bar:
        if len(row) < 7:
            raise DataIntegrityError("malformed Upstox candle")
        ts = datetime.fromisoformat(str(row[0]))
        bar = Bar(ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), int(row[5]), int(row[6]))
        if ts.tzinfo is None or min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
            raise DataIntegrityError("invalid Upstox candle")
        return bar

    def _exclude(self, session: date, symbol: str, reason: str) -> None:
        self.exclusions.append({"date": session.isoformat(), "symbol": symbol, "reason": reason})


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def group_bars(rows: Sequence[Bar]) -> dict[date, list[Bar]]:
    out: dict[date, list[Bar]] = defaultdict(list)
    for row in rows:
        out[row.timestamp.astimezone(IST).date()].append(row)
    return {key: sorted(value, key=lambda x: x.timestamp) for key, value in out.items()}


def has_gap(rows: Sequence[Bar], start_index: int, end_index: int) -> bool:
    if start_index < 0 or end_index >= len(rows) or start_index > end_index:
        return True
    return any(rows[i].timestamp - rows[i - 1].timestamp != timedelta(minutes=5) for i in range(start_index + 1, end_index + 1))


def clustered_bootstrap_ci(trades: Sequence[Trade], *, seed: int, resamples: int = 10_000) -> tuple[float, float]:
    if resamples < 10_000:
        raise ValueError("at least 10,000 bootstrap resamples are required")
    clusters: dict[date, list[float]] = defaultdict(list)
    for trade in trades:
        clusters[trade.session_date].append(trade.net_expectancy)
    days = sorted(clusters)
    if not days:
        return math.nan, math.nan
    rng = random.Random(seed)
    estimates = []
    for _ in range(resamples):
        sample = [rng.choice(days) for _ in days]
        values = [value for day in sample for value in clusters[day]]
        estimates.append(statistics.fmean(values))
    estimates.sort()
    return estimates[int(resamples * 0.025)], estimates[int(resamples * 0.975)]


def metrics(trades: Sequence[Trade], *, starting_capital: float, seed: int) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda x: (x.closed_at, x.opened_at, x.symbol))
    wins = [x.net_pnl for x in ordered if x.net_pnl > 0]
    losses = [-x.net_pnl for x in ordered if x.net_pnl < 0]
    equity = starting_capital
    peak = equity
    max_dd = 0.0
    for trade in ordered:
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 1.0)
    ci = clustered_bootstrap_ci(ordered, seed=seed)
    return {
        "trades": len(ordered),
        "days": len({x.session_date for x in ordered}),
        "win_rate_pct": 100.0 * len(wins) / len(ordered) if ordered else 0.0,
        "profit_factor": sum(wins) / sum(losses) if losses else (999999.0 if wins else 0.0),
        "mean_net_expectancy_inr": statistics.fmean(x.net_expectancy for x in ordered) if ordered else 0.0,
        "max_drawdown_pct": 100.0 * max_dd,
        "expectancy_ci_95_inr": (
            [ci[0], ci[1]] if ordered else [None, None]
        ),
    }


def split_dates(sessions: Sequence[date]) -> dict[str, set[date]]:
    ordered = sorted(set(sessions))
    train_end = int(len(ordered) * 0.60)
    validation_end = int(len(ordered) * 0.80)
    return {
        "train": set(ordered[:train_end]),
        "validation": set(ordered[train_end:validation_end]),
        "holdout": set(ordered[validation_end:]),
    }


def canonical_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _daily(row: Mapping[str, str], session: date, expiry: date | None) -> DailyBar:
    return DailyBar(
        session_date=session,
        symbol=row["TckrSymb"].strip().upper(),
        instrument_id=row["FinInstrmId"].strip(),
        isin=row.get("ISIN", "").strip(),
        expiry=expiry,
        open=float(row["OpnPric"]), high=float(row["HghPric"]), low=float(row["LwPric"]), close=float(row["ClsPric"]),
        volume=int(float(row["TtlTradgVol"] or 0)), oi=int(float(row["OpnIntrst"] or 0)), lot_size=int(float(row["NewBrdLotQty"] or 1)),
    )
