from __future__ import annotations

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
from daybagger.intelligence.meta_features import MetaFeatureError, build_cross_section_state, build_meta_raw_features
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
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip() or read_env_value(REPO_ROOT / ".env.local", "UPSTOX_ACCESS_TOKEN")
    if not token:
        raise SystemExit("UPSTOX_ACCESS_TOKEN missing")

    symbols = tuple(DEFAULT_META_VALIDATION_SYMBOLS)
    instruments = {item.trading_symbol: item for item in NSEEquityUniverse().load_mis_equities()}
    selected = {symbol: instruments[symbol] for symbol in symbols if symbol in instruments}

    market = UpstoxMarketData(access_token=token)
    client = HistoricalCandleClient(market, cache_dir=REPO_ROOT / "data" / "historical_cache")
    
    to_date = date(2026, 9, 7)
    from_date = date(2026, 8, 15)
    fetch_from = from_date - timedelta(days=35)

    market_by_day = grouped(client.fetch(NIFTY_KEY, from_date=fetch_from, to_date=to_date))
    bank_by_day = grouped(client.fetch(BANK_NIFTY_KEY, from_date=fetch_from, to_date=to_date))
    vix_by_day = grouped(client.fetch(INDIA_VIX_KEY, from_date=fetch_from, to_date=to_date))
    stocks = {symbol: grouped(client.fetch(item.instrument_key, from_date=fetch_from, to_date=to_date)) for symbol, item in selected.items()}

    sectors = load_sector_cache(REPO_ROOT / "data" / "sector_cache.json")
    sector_by_symbol = {symbol: sectors.get(item.isin, "") for symbol, item in selected.items()}
    cost_bps = IndiaEquityIntradayCostModel().conservative_linear_round_trip_bps()

    all_days = sorted(set(market_by_day) & set(bank_by_day) & set(vix_by_day))
    trading_days = [d for d in all_days if d >= from_date][-10:]

    print(f"=== TASK 1: REJECT-REASON DISTRIBUTION ACROSS LAST {len(trading_days)} TRADING SESSIONS ===")
    print(f"Date Range: {trading_days[0]} to {trading_days[-1]}\n")

    session_reports = []

    for session_date in trading_days:
        market_candles = market_by_day[session_date]
        bank_candles = bank_by_day[session_date]
        vix_candles = vix_by_day[session_date]
        
        if min(len(market_candles), len(bank_candles), len(vix_candles)) < 30:
            continue

        # 15-minute evaluation cadence matching validator
        decision_times = [market_candles[i].timestamp for i in range(29, len(market_candles), 15)]

        prefixes = {symbol: rows[session_date] for symbol, rows in stocks.items() if session_date in rows and len(rows[session_date]) >= 30 and sector_by_symbol.get(symbol)}
        if len(prefixes) < 6:
            continue

        reasons_counter = Counter()
        total_evaluations = 0

        for as_of in decision_times:
            stock_prefixes = {symbol: [c for c in rows if c.timestamp <= as_of] for symbol, rows in prefixes.items()}
            stock_prefixes = {symbol: rows for symbol, rows in stock_prefixes.items() if rows and rows[-1].timestamp == as_of}
            if len(stock_prefixes) < 6:
                continue

            try:
                cross = build_cross_section_state(session_date=session_date, as_of=as_of, prefixes_by_symbol=stock_prefixes, sector_by_symbol=sector_by_symbol)
            except MetaFeatureError:
                continue

            for symbol, prefix in stock_prefixes.items():
                prior_dates = [d for d in sorted(stocks[symbol]) if d < session_date][-20:]
                prior = [stocks[symbol][d] for d in prior_dates]
                if len(prior) < 5:
                    continue
                try:
                    raw = build_meta_raw_features(
                        symbol=symbol,
                        stock_prefix=prefix,
                        market_prefix=[c for c in market_candles if c.timestamp <= as_of],
                        bank_nifty_prefix=[c for c in bank_candles if c.timestamp <= as_of],
                        india_vix_prefix=[c for c in vix_candles if c.timestamp <= as_of],
                        cross_section=cross,
                        sector=sector_by_symbol[symbol],
                        prior_stock_sessions=prior,
                    )
                    decision = decide_baseline(
                        symbol=symbol,
                        as_of=as_of,
                        raw_features=raw,
                        statutory_cost_bps=cost_bps,
                        live_spread_bps=4.0,
                        paper_slippage_bps_per_side=2.0,
                    )
                    total_evaluations += 1
                    status = decision.opportunity.status.value
                    if status != "QUALIFIED":
                        reasons_counter[decision.opportunity.reason] += 1
                    else:
                        reasons_counter["QUALIFIED"] += 1
                except Exception:
                    reasons_counter["INSUFFICIENT_EVIDENCE"] += 1

        if total_evaluations == 0:
            continue

        rank_regime_count = (
            reasons_counter["BASELINE_LONG_REGIME_OR_RANK_GATE"] +
            reasons_counter["BASELINE_SHORT_REGIME_OR_RANK_GATE"] +
            reasons_counter["BASELINE_REGIME_NOT_TRENDING"]
        )
        rank_regime_pct = (rank_regime_count / total_evaluations * 100.0) if total_evaluations > 0 else 0.0

        rep = {
            "date": session_date.isoformat(),
            "total_evaluations": total_evaluations,
            "rank_regime_count": rank_regime_count,
            "rank_regime_pct": round(rank_regime_pct, 2),
            "reasons": dict(sorted(reasons_counter.items())),
        }
        session_reports.append(rep)
        print(f"Session Date: {rep['date']} | Total Evaluated: {total_evaluations} | Rank/Regime Rejects: {rank_regime_count} ({rep['rank_regime_pct']}%)")
        for k, v in rep["reasons"].items():
            pct = round(v / total_evaluations * 100.0, 1) if total_evaluations > 0 else 0.0
            print(f"   - {k}: {v} ({pct}%)")
        print("-" * 65)

    chronic_count = sum(1 for r in session_reports if r["rank_regime_pct"] > 90.0)
    print(f"\nSUMMARY: {chronic_count} out of {len(session_reports)} sessions had >90% rank/regime rejects.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
