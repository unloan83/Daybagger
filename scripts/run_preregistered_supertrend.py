from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.config import load_settings
from daybagger.data.universe import NSEEquityUniverse
from daybagger.data.upstox import UpstoxMarketData
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.runtime.local_env import load_env_value
from daybagger.validation.historical import HistoricalCandleClient
from daybagger.validation.meta_intelligence import _spearman
from daybagger.validation.default_meta_universe import DEFAULT_META_VALIDATION_SYMBOLS

INDIA = ZoneInfo("Asia/Kolkata")
FROM_DATE = date(2026, 1, 5)
TO_DATE = date(2026, 3, 31)
ATR_LOOKBACK = 14
MULTIPLIER = 3.0
HORIZON_MINUTES = 15
WARMUP_BARS = 30


def _atr(candles):
    values = []
    for index, candle in enumerate(candles):
        previous_close = candles[index - 1].close if index else candle.open
        true_range = max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        )
        values.append(true_range)
    if len(values) < ATR_LOOKBACK:
        return None
    return sum(values[-ATR_LOOKBACK:], Decimal("0")) / Decimal(ATR_LOOKBACK)


def _supertrend(candles):
    atr = _atr(candles)
    if atr is None or atr <= 0:
        return None
    current = candles[-1]
    midpoint = (current.high + current.low) / Decimal("2")
    upper = midpoint + Decimal(str(MULTIPLIER)) * atr
    lower = midpoint - Decimal(str(MULTIPLIER)) * atr
    close = current.close
    if close > upper:
        direction = 1
        line = lower
    elif close < lower:
        direction = -1
        line = upper
    else:
        direction = 1 if close >= midpoint else -1
        line = lower if direction == 1 else upper
    distance_bps = float(abs(close - line) / close * Decimal("10000"))
    return direction, atr, line, distance_bps


def _triple_barrier(entry, bars, direction, atr):
    stop = atr
    target = atr * Decimal("2")
    stop_price = entry - stop if direction == 1 else entry + stop
    target_price = entry + target if direction == 1 else entry - target
    for bar in bars:
        if direction == 1:
            if bar.low <= stop_price:
                return -float(stop / entry * Decimal("10000"))
            if bar.high >= target_price:
                return float(target / entry * Decimal("10000"))
        else:
            if bar.high >= stop_price:
                return -float(stop / entry * Decimal("10000"))
            if bar.low <= target_price:
                return float(target / entry * Decimal("10000"))
    exit_price = bars[-1].close
    raw = (exit_price / entry - Decimal("1")) * Decimal("10000")
    return float(raw if direction == 1 else -raw)


def _group(candles):
    grouped = {}
    for candle in candles:
        grouped.setdefault(candle.timestamp.astimezone(INDIA).date(), []).append(candle)
    return {day: sorted(rows, key=lambda row: row.timestamp) for day, rows in grouped.items()}


def main() -> int:
    token = load_env_value(
        "UPSTOX_ACCESS_TOKEN",
        REPO_ROOT / ".env.local",
        REPO_ROOT / ".env",
    )
    if not token:
        raise SystemExit("UPSTOX_ACCESS_TOKEN missing")
    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    cost_model = IndiaEquityIntradayCostModel()
    cost_bps = (
        cost_model.round_trip_bps_for_notional(
            Decimal(str(settings.capital.starting_capital_inr))
            * Decimal(str(settings.risk.max_position_fraction))
        )
        + 2.0 * settings.execution.paper_slippage_bps
    )
    market = UpstoxMarketData(access_token=token)
    historical = HistoricalCandleClient(
        market, cache_dir=REPO_ROOT / "data" / "historical_cache"
    )
    instruments = NSEEquityUniverse().load_mis_equities()
    by_symbol = {item.trading_symbol.upper(): item for item in instruments}
    selected = [
        by_symbol[symbol]
        for symbol in DEFAULT_META_VALIDATION_SYMBOLS
        if symbol in by_symbol
    ]
    rows = []
    for instrument in selected:
        sessions = _group(
            historical.fetch(
                instrument.instrument_key,
                from_date=FROM_DATE,
                to_date=TO_DATE,
            )
        )
        for day, candles in sessions.items():
            if len(candles) < WARMUP_BARS + HORIZON_MINUTES + 1:
                continue
            for index in range(WARMUP_BARS - 1, len(candles) - HORIZON_MINUTES, HORIZON_MINUTES):
                prefix = candles[: index + 1]
                signal = _supertrend(prefix)
                if signal is None:
                    continue
                direction, atr, line, distance_bps = signal
                entry_bar = candles[index + 1]
                exit_bars = candles[index + 1 : index + HORIZON_MINUTES + 1]
                expected_gross = float((atr * Decimal("2")) / entry_bar.open * Decimal("10000"))
                expected_net = expected_gross - cost_bps
                realised = _triple_barrier(entry_bar.open, exit_bars, direction, atr)
                rows.append({
                    "symbol": instrument.trading_symbol,
                    "as_of": prefix[-1].timestamp.isoformat(),
                    "score": direction * distance_bps,
                    "expected_net_bps": expected_net,
                    "realised_net_bps": realised - cost_bps,
                })

    positive = [row for row in rows if row["expected_net_bps"] > 0]
    realised = [row["realised_net_bps"] for row in positive]
    rank_ic = _spearman(
        [row["score"] for row in positive],
        realised,
    ) if len(positive) >= 3 else 0.0
    summary = {
        "experiment": "PREREGISTERED_SUPERTREND_ATR",
        "from_date": FROM_DATE.isoformat(),
        "to_date": TO_DATE.isoformat(),
        "atr_lookback": ATR_LOOKBACK,
        "multiplier": MULTIPLIER,
        "horizon_minutes": HORIZON_MINUTES,
        "cost_bps": cost_bps,
        "scored_rows": len(rows),
        "positive_edge_rows": len(positive),
        "mean_realised_net_bps": mean(realised) if realised else 0.0,
        "rank_ic": rank_ic,
        "pass": len(positive) >= 20 and (mean(realised) if realised else 0.0) >= 10.0 and rank_ic >= 0.05,
    }
    output = REPO_ROOT / "research" / "evidence" / "preregistered_supertrend_atr.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
