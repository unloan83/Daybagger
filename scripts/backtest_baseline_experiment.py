from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.config import load_settings
from daybagger.data.universe import NSEEquityUniverse
from daybagger.data.upstox import IntradayCandle, UpstoxMarketData
from daybagger.decision.baseline import BASELINE_HORIZON_MINUTES, decide_baseline
from daybagger.domain import DecisionStatus, Direction
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.intelligence.meta_features import build_cross_section_state, build_meta_raw_features
from daybagger.intelligence.upstox_external import UpstoxExternalIntelligence, load_sector_cache, save_sector_cache
from daybagger.runtime.local_env import read_env_value
from daybagger.validation.default_meta_universe import DEFAULT_META_VALIDATION_SYMBOLS
from daybagger.validation.historical import HistoricalCandleClient

INDIA = ZoneInfo("Asia/Kolkata")
NIFTY_KEY = "NSE_INDEX|Nifty 50"
BANK_NIFTY_KEY = "NSE_INDEX|Nifty Bank"
INDIA_VIX_KEY = "NSE_INDEX|India VIX"


def group_sessions(candles: list[IntradayCandle]) -> dict[date, list[IntradayCandle]]:
    result: dict[date, list[IntradayCandle]] = {}
    for candle in sorted(candles, key=lambda c: c.timestamp):
        local_date = candle.timestamp.astimezone(INDIA).date()
        result.setdefault(local_date, []).append(candle)
    return result


def compute_spearman_rank_ic(x: list[float], y: list[float]) -> float:
    if len(x) < 5 or len(x) != len(y):
        return 0.0

    def rank(arr: list[float]) -> list[float]:
        sorted_indices = sorted(range(len(arr)), key=lambda i: arr[i])
        ranks = [0.0] * len(arr)
        for r, idx in enumerate(sorted_indices):
            ranks[idx] = float(r + 1)
        return ranks

    rx = rank(x)
    ry = rank(y)
    mean_rx = mean(rx)
    mean_ry = mean(ry)

    num = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(len(rx)))
    den_x = math.sqrt(sum((rx[i] - mean_rx) ** 2 for i in range(len(rx))))
    den_y = math.sqrt(sum((ry[i] - mean_ry) ** 2 for i in range(len(ry))))

    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


@dataclass
class SampleResult:
    session_date: date
    symbol: str
    as_of: datetime
    status: DecisionStatus
    reason: str
    expected_net_bps: float
    confidence: float
    realised_gross_bps: float
    realised_net_bps: float
    is_holdout: bool


def main() -> int:
    print("=== TASK 2: PRE-REGISTERED EXPERIMENT BACKTEST ===", flush=True)
    verify_golden_rules(REPO_ROOT)

    # Fresh historical window: 2026-07-01 to 2026-09-04
    from_date = date(2026, 7, 1)
    to_date = date(2026, 9, 4)

    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip() or read_env_value(REPO_ROOT / ".env.local", "UPSTOX_ACCESS_TOKEN")
    if not token:
        raise SystemExit("UPSTOX_ACCESS_TOKEN missing from local .env.local")

    market_data = UpstoxMarketData(access_token=token)
    official = NSEEquityUniverse().load_mis_equities()
    by_symbol = {item.trading_symbol.upper(): item for item in official}
    selected = [by_symbol[s] for s in DEFAULT_META_VALIDATION_SYMBOLS if s in by_symbol]

    external = UpstoxExternalIntelligence(market_data)
    cache_path = REPO_ROOT / "data" / "sector_cache.json"
    cache = load_sector_cache(cache_path)
    sectors: dict[str, str] = {}
    for instrument in selected:
        sector = cache.get(instrument.isin)
        if not sector:
            sector = external.company_sector(instrument.isin)
            cache[instrument.isin] = sector
        sectors[instrument.trading_symbol] = sector

    historical = HistoricalCandleClient(market_data, cache_dir=REPO_ROOT / "data" / "historical_cache")

    fetch_from = from_date - timedelta(days=35)
    print("Fetching historical candles for market indices and symbols...", flush=True)
    contexts = {
        "market": group_sessions(historical.fetch(NIFTY_KEY, from_date=fetch_from, to_date=to_date)),
        "bank": group_sessions(historical.fetch(BANK_NIFTY_KEY, from_date=fetch_from, to_date=to_date)),
        "vix": group_sessions(historical.fetch(INDIA_VIX_KEY, from_date=fetch_from, to_date=to_date)),
    }

    stocks: dict[str, dict[date, list[IntradayCandle]]] = {}
    for instrument in selected:
        candles = historical.fetch(instrument.instrument_key, from_date=fetch_from, to_date=to_date)
        grouped = group_sessions(candles)
        if grouped:
            stocks[instrument.trading_symbol] = grouped

    usable = tuple(sorted(stocks))
    
    # 20% unseen symbol holdout cohort (deterministic)
    symbol_holdout = set(symbol for idx, symbol in enumerate(usable) if idx % 5 == 0)

    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    cost_model = IndiaEquityIntradayCostModel()
    position_notional = Decimal("30000") * Decimal(str(settings.risk.max_position_fraction))
    statutory_cost_bps = cost_model.round_trip_bps_for_notional(position_notional)
    paper_slippage_bps = settings.execution.paper_slippage_bps

    common_dates = sorted(set(contexts["market"]).intersection(contexts["bank"]).intersection(contexts["vix"]))
    eval_dates = [d for d in common_dates if d >= from_date]
    
    # Final 15 sessions holdout
    final_holdout_sessions = 15
    holdout_dates = set(eval_dates[-final_holdout_sessions:])

    results: list[SampleResult] = []

    print(f"Evaluating fresh historical window: {from_date} to {to_date} ({len(eval_dates)} trading sessions)", flush=True)

    for session_idx, session_date in enumerate(eval_dates):
        market = sorted(contexts["market"].get(session_date, ()), key=lambda c: c.timestamp)
        bank = sorted(contexts["bank"].get(session_date, ()), key=lambda c: c.timestamp)
        vix = sorted(contexts["vix"].get(session_date, ()), key=lambda c: c.timestamp)
        if min(len(market), len(bank), len(vix)) < 30:
            continue

        market_by_ts = {c.timestamp: i for i, c in enumerate(market)}
        bank_by_ts = {c.timestamp: i for i, c in enumerate(bank)}
        vix_by_ts = {c.timestamp: i for i, c in enumerate(vix)}
        decision_times = [market[i].timestamp for i in range(29, len(market), 15)]

        stock_for_day = {
            symbol: sorted(stocks[symbol].get(session_date, ()), key=lambda c: c.timestamp)
            for symbol in usable
        }
        stock_index = {
            symbol: {c.timestamp: i for i, c in enumerate(candles)}
            for symbol, candles in stock_for_day.items() if candles
        }

        for as_of in decision_times:
            if as_of not in bank_by_ts or as_of not in vix_by_ts:
                continue
            prefixes = {}
            for symbol, candles in stock_for_day.items():
                idx = stock_index.get(symbol, {}).get(as_of)
                if idx is not None and idx >= 29:
                    prefixes[symbol] = candles[: idx + 1]
            if len(prefixes) < 6:
                continue

            try:
                cross = build_cross_section_state(
                    session_date=session_date,
                    as_of=as_of,
                    prefixes_by_symbol=prefixes,
                    sector_by_symbol=sectors,
                )
            except Exception:
                continue

            market_prefix = market[: market_by_ts[as_of] + 1]
            bank_prefix = bank[: bank_by_ts[as_of] + 1]
            vix_prefix = vix[: vix_by_ts[as_of] + 1]

            for symbol, stock_prefix in prefixes.items():
                prior_dates = [d for d in sorted(stocks[symbol]) if d < session_date][-20:]
                prior = [stocks[symbol][d] for d in prior_dates]
                if len(prior) < 5:
                    continue

                try:
                    raw = build_meta_raw_features(
                        symbol=symbol,
                        stock_prefix=stock_prefix,
                        market_prefix=market_prefix,
                        bank_nifty_prefix=bank_prefix,
                        india_vix_prefix=vix_prefix,
                        cross_section=cross,
                        sector=sectors[symbol],
                        prior_stock_sessions=prior,
                    )
                    decision = decide_baseline(
                        symbol=symbol,
                        as_of=as_of,
                        raw_features=raw,
                        statutory_cost_bps=statutory_cost_bps,
                        live_spread_bps=4.0,  # Declared paper spread allowance
                        paper_slippage_bps_per_side=paper_slippage_bps,
                    )
                except Exception:
                    continue

                candles = stock_for_day[symbol]
                idx = stock_index[symbol][as_of]
                entry_idx = idx + 1
                if entry_idx >= len(candles):
                    continue
                exit_ts = as_of + timedelta(minutes=BASELINE_HORIZON_MINUTES)
                exit_idx = stock_index[symbol].get(exit_ts)
                if exit_idx is None or exit_idx < entry_idx or exit_ts.astimezone(INDIA).time() > time(15, 10):
                    continue

                entry_p = float(candles[entry_idx].open)
                exit_p = float(candles[exit_idx].close)
                if entry_p <= 0:
                    continue

                direction = decision.opportunity.direction
                if direction == Direction.LONG:
                    gross_bps = (exit_p - entry_p) / entry_p * 10000.0
                elif direction == Direction.SHORT:
                    gross_bps = (entry_p - exit_p) / entry_p * 10000.0
                else:
                    res_score = 0.5 * (float(raw.get("rs_vs_benchmark_bps", 0)) + float(raw.get("rs_vs_sector_bps", 0)))
                    if res_score > 0:
                        gross_bps = (exit_p - entry_p) / entry_p * 10000.0
                    else:
                        gross_bps = (entry_p - exit_p) / entry_p * 10000.0

                total_cost_bps = statutory_cost_bps + 4.0 + 2.0 * paper_slippage_bps
                net_bps = gross_bps - total_cost_bps

                is_holdout = (session_date in holdout_dates) or (symbol in symbol_holdout)

                results.append(
                    SampleResult(
                        session_date=session_date,
                        symbol=symbol,
                        as_of=as_of,
                        status=decision.opportunity.status,
                        reason=decision.opportunity.reason,
                        expected_net_bps=decision.opportunity.expected_net_return_bps,
                        confidence=decision.opportunity.confidence,
                        realised_gross_bps=gross_bps,
                        realised_net_bps=net_bps,
                        is_holdout=is_holdout,
                    )
                )

        if (session_idx + 1) % 5 == 0 or session_idx == len(eval_dates) - 1:
            print(f"Progress: {session_idx + 1}/{len(eval_dates)} sessions evaluated (Total rows: {len(results)})", flush=True)

    total_scored = len(results)
    qualified_rows = [r for r in results if r.status == DecisionStatus.QUALIFIED]
    positive_edge_rows = len(qualified_rows)

    dev_results = [r for r in results if not r.is_holdout]
    holdout_results = [r for r in results if r.is_holdout]

    holdout_qualified = [r for r in holdout_results if r.status == DecisionStatus.QUALIFIED]
    
    if holdout_qualified:
        holdout_net_return = mean(r.realised_net_bps for r in holdout_qualified)
    else:
        holdout_net_return = 0.0

    holdout_preds = [r.expected_net_bps for r in holdout_results]
    holdout_actuals = [r.realised_net_bps for r in holdout_results]
    rank_ic = compute_spearman_rank_ic(holdout_preds, holdout_actuals)

    report = {
        "experiment_name": "DECIDE_BASELINE_PRE_REGISTERED_BACKTEST",
        "window": {
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "total_eval_sessions": len(eval_dates),
            "final_holdout_sessions": final_holdout_sessions,
        },
        "cost_model": {
            "position_notional_inr": float(position_notional),
            "statutory_cost_bps": round(statutory_cost_bps, 4),
            "paper_slippage_bps_per_side": paper_slippage_bps,
            "total_cost_hurdle_bps": round(statutory_cost_bps + 4.0 + 2.0 * paper_slippage_bps, 4),
        },
        "metrics": {
            "total_scored_rows": total_scored,
            "positive_edge_rows": positive_edge_rows,
            "holdout_scored_rows": len(holdout_results),
            "holdout_qualified_rows": len(holdout_qualified),
            "holdout_net_return_bps": round(holdout_net_return, 4),
            "holdout_rank_ic": round(rank_ic, 4),
        },
    }

    print("\nDAYBAGGER PRE-REGISTERED EXPERIMENT REPORT:", flush=True)
    print(json.dumps(report, indent=2), flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
