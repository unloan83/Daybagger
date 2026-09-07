from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.data.universe import NSEEquityUniverse
from daybagger.data.upstox import UpstoxMarketData
from daybagger.decision.baseline import decide_baseline
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.intelligence.meta_features import build_cross_section_state, build_meta_raw_features
from daybagger.intelligence.upstox_external import load_sector_cache
from daybagger.runtime.local_env import read_env_value
from daybagger.validation.default_meta_universe import DEFAULT_META_VALIDATION_SYMBOLS
from daybagger.validation.historical import HistoricalCandleClient

INDIA = ZoneInfo("Asia/Kolkata")
NIFTY_KEY = "NSE_INDEX|Nifty 50"
BANK_NIFTY_KEY = "NSE_INDEX|Nifty Bank"
INDIA_VIX_KEY = "NSE_INDEX|India VIX"


def grouped(candles):
    result = {}
    for candle in candles:
        result.setdefault(candle.timestamp.astimezone(INDIA).date(), []).append(candle)
    return {day: sorted(rows, key=lambda row: row.timestamp) for day, rows in result.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay the canonical baseline on genuine historical candles.")
    parser.add_argument("--from-date", type=date.fromisoformat, default=date.today() - timedelta(days=35))
    parser.add_argument("--to-date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--spread-bps", type=float, default=4.0, help="Declared historical spread scenario; no bid/ask is fabricated.")
    parser.add_argument("--symbol", action="append", dest="symbols")
    args = parser.parse_args()
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip() or read_env_value(REPO_ROOT / ".env.local", "UPSTOX_ACCESS_TOKEN")
    if not token:
        raise SystemExit("UPSTOX_ACCESS_TOKEN missing")
    if args.spread_bps < 0:
        raise SystemExit("--spread-bps must be non-negative")
    symbols = tuple(args.symbols or DEFAULT_META_VALIDATION_SYMBOLS)
    instruments = {item.trading_symbol: item for item in NSEEquityUniverse().load_mis_equities()}
    selected = {symbol: instruments[symbol] for symbol in symbols if symbol in instruments}
    if len(selected) < 6:
        raise SystemExit("fewer than six requested symbols are in the official universe")
    market = UpstoxMarketData(access_token=token)
    client = HistoricalCandleClient(market, cache_dir=REPO_ROOT / "data" / "historical_cache")
    fetch_from = args.from_date - timedelta(days=35)
    market_by_day = grouped(client.fetch(NIFTY_KEY, from_date=fetch_from, to_date=args.to_date))
    bank_by_day = grouped(client.fetch(BANK_NIFTY_KEY, from_date=fetch_from, to_date=args.to_date))
    vix_by_day = grouped(client.fetch(INDIA_VIX_KEY, from_date=fetch_from, to_date=args.to_date))
    stocks = {symbol: grouped(client.fetch(item.instrument_key, from_date=fetch_from, to_date=args.to_date)) for symbol, item in selected.items()}
    sectors = load_sector_cache(REPO_ROOT / "data" / "sector_cache.json")
    sector_by_symbol = {symbol: sectors.get(item.isin, "") for symbol, item in selected.items()}
    cost_bps = IndiaEquityIntradayCostModel().conservative_linear_round_trip_bps()
    counts = Counter()
    scored = 0
    for session_date in sorted(set(market_by_day) & set(bank_by_day) & set(vix_by_day)):
        if not args.from_date <= session_date <= args.to_date:
            continue
        prefixes = {symbol: rows[session_date] for symbol, rows in stocks.items() if session_date in rows and len(rows[session_date]) >= 30 and sector_by_symbol[symbol]}
        if len(prefixes) < 6:
            counts["INSUFFICIENT_ALIGNED_MINUTE_DATA"] += 1
            continue
        timestamps = set(c.timestamp for c in market_by_day[session_date]) & set(c.timestamp for c in bank_by_day[session_date]) & set(c.timestamp for c in vix_by_day[session_date])
        for as_of in sorted(timestamps):
            stock_prefixes = {symbol: [c for c in rows if c.timestamp <= as_of] for symbol, rows in prefixes.items()}
            stock_prefixes = {symbol: rows for symbol, rows in stock_prefixes.items() if rows and rows[-1].timestamp == as_of}
            if len(stock_prefixes) < 6:
                continue
            cross = build_cross_section_state(session_date=session_date, as_of=as_of, prefixes_by_symbol=stock_prefixes, sector_by_symbol=sector_by_symbol)
            for symbol, prefix in stock_prefixes.items():
                prior = [rows[day] for day, rows in stocks[symbol].items() if day < session_date][-20:]
                if len(prior) < 5:
                    continue
                try:
                    raw = build_meta_raw_features(symbol=symbol, stock_prefix=prefix, market_prefix=[c for c in market_by_day[session_date] if c.timestamp <= as_of], bank_nifty_prefix=[c for c in bank_by_day[session_date] if c.timestamp <= as_of], india_vix_prefix=[c for c in vix_by_day[session_date] if c.timestamp <= as_of], cross_section=cross, sector=sector_by_symbol[symbol], prior_stock_sessions=prior)
                    decision = decide_baseline(symbol=symbol, as_of=as_of, raw_features=raw, statutory_cost_bps=cost_bps, live_spread_bps=args.spread_bps, paper_slippage_bps_per_side=2.0)
                except Exception:
                    counts["INSUFFICIENT_EVIDENCE"] += 1
                    continue
                scored += 1
                counts[decision.opportunity.status.value] += 1
                if decision.opportunity.status.value != "QUALIFIED":
                    counts[decision.opportunity.reason] += 1
    print(json.dumps({"from_date": args.from_date.isoformat(), "to_date": args.to_date.isoformat(), "spread_scenario_bps": args.spread_bps, "scored_rows": scored, "counts": dict(sorted(counts.items())), "paper_only": True}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
