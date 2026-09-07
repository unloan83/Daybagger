from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean
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
class TradeSample:
    session_date: date
    symbol: str
    as_of: datetime
    status: DecisionStatus
    direction: Direction
    expected_net_bps: float
    confidence: float
    realised_gross_bps: float
    realised_net_bps: float
    is_holdout: bool


def load_pcr_regime_map(oi_json_path: Path) -> dict[date, dict]:
    if not oi_json_path.exists():
        raise FileNotFoundError(f"Missing OI dataset: {oi_json_path}")
    raw_list = json.loads(oi_json_path.read_text(encoding="utf-8"))
    
    sorted_recs = sorted(raw_list, key=lambda r: r["date"])
    out: dict[date, dict] = {}
    
    pcr_history = []
    for i, r in enumerate(sorted_recs):
        d = date.fromisoformat(r["date"])
        pcr = r.get("nifty", {}).get("pcr")
        if pcr is None:
            continue
        pcr_history.append((d, pcr))
        
        # Calculate 5-session PCR trend: (PCR_t - PCR_{t-5})
        pcr_trend_5d = 0.0
        if len(pcr_history) >= 6:
            pcr_trend_5d = pcr - pcr_history[-6][1]
            
        out[d] = {
            "pcr": pcr,
            "pcr_trend_5d": pcr_trend_5d,
            "is_oversold_bullish": pcr < 0.70,
            "is_overbought_bearish": pcr > 1.30,
        }
    return out


def evaluate_pcr_gate(direction: Direction, prior_eod_pcr: dict | None) -> bool:
    """
    Returns True if trade direction passes the EOD PCR macro regime filter.
    Rules:
    1. If prior EOD PCR is extreme oversold (< 0.70), reject SHORT trades.
    2. If prior EOD PCR is extreme overbought (> 1.30), reject LONG trades.
    3. If 5-day PCR trend is falling (< -0.10), reject LONG trades.
    4. If 5-day PCR trend is rising (> +0.10), reject SHORT trades.
    """
    if not prior_eod_pcr:
        return True  # If no prior day PCR, neutral pass

    pcr = prior_eod_pcr["pcr"]
    pcr_trend = prior_eod_pcr["pcr_trend_5d"]

    if direction == Direction.LONG:
        if pcr > 1.30 or pcr_trend < -0.10:
            return False  # Adverse bearish regime gate
    elif direction == Direction.SHORT:
        if pcr < 0.70 or pcr_trend > 0.10:
            return False  # Adverse bullish regime gate

    return True


def main() -> int:
    verify_golden_rules(REPO_ROOT)

    print("=========================================================================")
    print(" DAYBAGGER PRE-REGISTERED OI PCR REGIME EXPERIMENT BACKTEST")
    print("=========================================================================")
    print("Hypothesis: Prior-day EOD NIFTY PCR level & 5-day trend gate next-session")
    print("            trade directions, filtering out adverse macro regime trades.")
    print("Pass Bar: Holdout Net Return Lift > +5.0 bps, Rank IC Lift > +0.05.")
    print("Kill Condition: Holdout Net Return Lift <= 0.0 bps OR Rank IC Lift <= 0.0.")
    print("=========================================================================\n")

    oi_path = REPO_ROOT / "data" / "historical_oi_12m.json"
    pcr_map = load_pcr_regime_map(oi_path)
    sorted_pcr_dates = sorted(pcr_map.keys())
    print(f"Loaded 12-month NIFTY EOD PCR map across {len(pcr_map)} sessions ({sorted_pcr_dates[0]} to {sorted_pcr_dates[-1]})")

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

    from_date = sorted_pcr_dates[0]
    to_date = sorted_pcr_dates[-1]
    fetch_from = from_date - timedelta(days=35)

    print(f"Fetching 12-month intraday candle history from {from_date} to {to_date}...", flush=True)

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
    symbol_holdout = set(symbol for idx, symbol in enumerate(usable) if idx % 5 == 0)

    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    cost_model = IndiaEquityIntradayCostModel()
    position_notional = Decimal("30000") * Decimal(str(settings.risk.max_position_fraction))
    statutory_cost_bps = cost_model.round_trip_bps_for_notional(position_notional)
    paper_slippage_bps = settings.execution.paper_slippage_bps
    total_cost_hurdle_bps = statutory_cost_bps + 4.0 + 2.0 * paper_slippage_bps

    common_dates = sorted(set(contexts["market"]).intersection(contexts["bank"]).intersection(contexts["vix"]))
    eval_dates = [d for d in common_dates if d >= from_date]

    final_holdout_sessions = 30
    holdout_dates = set(eval_dates[-final_holdout_sessions:])

    samples: list[TradeSample] = []

    print(f"Evaluating {len(eval_dates)} trading sessions across {len(usable)} universe equities...", flush=True)

    for session_idx, session_date in enumerate(eval_dates):
        # Determine prior trading day EOD PCR
        prior_dates = [d for d in sorted_pcr_dates if d < session_date]
        prior_pcr_data = pcr_map.get(prior_dates[-1]) if prior_dates else None

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
                p_dates = [d for d in sorted(stocks[symbol]) if d < session_date][-20:]
                prior_stock = [stocks[symbol][d] for d in p_dates]
                if len(prior_stock) < 5:
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
                        prior_stock_sessions=prior_stock,
                    )
                    decision = decide_baseline(
                        symbol=symbol,
                        as_of=as_of,
                        raw_features=raw,
                        statutory_cost_bps=statutory_cost_bps,
                        live_spread_bps=4.0,
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

                net_bps = gross_bps - total_cost_hurdle_bps
                is_holdout = (session_date in holdout_dates) or (symbol in symbol_holdout)

                # Check PCR regime gate
                pcr_passed = evaluate_pcr_gate(direction, prior_pcr_data)

                # If PCR gate fails, the decision is rejected by regime
                status = decision.opportunity.status
                if status == DecisionStatus.QUALIFIED and not pcr_passed:
                    status = DecisionStatus.REJECTED

                samples.append(
                    TradeSample(
                        session_date=session_date,
                        symbol=symbol,
                        as_of=as_of,
                        status=status,
                        direction=direction,
                        expected_net_bps=decision.opportunity.expected_net_return_bps,
                        confidence=decision.opportunity.confidence,
                        realised_gross_bps=gross_bps,
                        realised_net_bps=net_bps,
                        is_holdout=is_holdout,
                    )
                )

        if (session_idx + 1) % 25 == 0 or session_idx == len(eval_dates) - 1:
            print(f"Progress: {session_idx + 1}/{len(eval_dates)} sessions evaluated ({len(samples)} trade rows scored)", flush=True)

    # Compute baseline metrics (without PCR gate filtering) vs PCR-Gated metrics
    # Baseline qualified: all where decision logic qualified without PCR gate
    all_scored = len(samples)
    dev_samples = [s for s in samples if not s.is_holdout]
    holdout_samples = [s for s in samples if s.is_holdout]

    # Holdout Baseline
    holdout_baseline_qual = [s for s in holdout_samples if s.expected_net_bps > 0 and s.confidence >= 0.5]
    holdout_baseline_net = mean(s.realised_net_bps for s in holdout_baseline_qual) if holdout_baseline_qual else 0.0

    # Holdout PCR-Gated
    holdout_pcr_qual = [s for s in holdout_samples if s.status == DecisionStatus.QUALIFIED]
    holdout_pcr_net = mean(s.realised_net_bps for s in holdout_pcr_qual) if holdout_pcr_qual else 0.0

    net_return_lift_bps = holdout_pcr_net - holdout_baseline_net

    # Rank IC Calculation
    baseline_preds = [s.expected_net_bps for s in holdout_samples]
    baseline_actuals = [s.realised_net_bps for s in holdout_samples]
    baseline_rank_ic = compute_spearman_rank_ic(baseline_preds, baseline_actuals)

    pcr_preds = [s.expected_net_bps if s.status == DecisionStatus.QUALIFIED else -999.0 for s in holdout_samples]
    pcr_actuals = [s.realised_net_bps for s in holdout_samples]
    pcr_rank_ic = compute_spearman_rank_ic(pcr_preds, pcr_actuals)

    rank_ic_lift = pcr_rank_ic - baseline_rank_ic

    passed = (net_return_lift_bps > 5.0) and (rank_ic_lift > 0.05)
    killed = (net_return_lift_bps <= 0.0) or (rank_ic_lift <= 0.0)

    report = {
        "experiment_name": "OI_PCR_REGIME_PRE_REGISTERED_BACKTEST_12M",
        "dataset": {
            "source": "NSE Historical F&O Bhavcopy (UDiFF)",
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "total_eval_sessions": len(eval_dates),
            "holdout_sessions": final_holdout_sessions,
        },
        "cost_model": {
            "position_notional_inr": float(position_notional),
            "statutory_cost_bps": round(statutory_cost_bps, 4),
            "paper_slippage_bps_per_side": paper_slippage_bps,
            "total_cost_hurdle_bps": round(total_cost_hurdle_bps, 4),
        },
        "results": {
            "total_scored_rows": all_scored,
            "holdout_scored_rows": len(holdout_samples),
            "baseline_holdout_qualified_rows": len(holdout_baseline_qual),
            "pcr_gated_holdout_qualified_rows": len(holdout_pcr_qual),
            "baseline_holdout_net_return_bps": round(holdout_baseline_net, 4),
            "pcr_gated_holdout_net_return_bps": round(holdout_pcr_net, 4),
            "holdout_net_return_lift_bps": round(net_return_lift_bps, 4),
            "baseline_rank_ic": round(baseline_rank_ic, 4),
            "pcr_gated_rank_ic": round(pcr_rank_ic, 4),
            "rank_ic_lift": round(rank_ic_lift, 4),
        },
        "evaluation": {
            "pass_bar_net_return_lift_bps": 5.0,
            "pass_bar_rank_ic_lift": 0.05,
            "experiment_status": "PASSED" if passed else ("KILLED" if killed else "FAILED_BAR"),
        },
    }

    print("\n=========================================================================")
    print(" PRE-REGISTERED EXPERIMENT REPORT (12-MONTH DATASET):")
    print("=========================================================================")
    print(json.dumps(report, indent=2))
    print("=========================================================================\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
