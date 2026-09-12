from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import json
import os
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from daybagger.engine.risk_gate import RiskDesk
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.validation.reconciled_cpr_oi import (
    IST, Bar, ReconciledDataPipeline, Trade, canonical_json_hash, group_bars,
    has_gap, metrics, split_dates,
)
from trading_contracts.schemas.v1 import Direction, MarketRegime, SignalCandidate
from trading_contracts.execution import liquidity_slippage_bps_per_side
import duckdb

BOOTSTRAP_SEED = 20260911
STARTING_CAPITAL = 100_000.0
SOURCE_COMMIT = "f8bfb5c2d3736d534fbfa81376a86523838f2f3c"
LOCKED_PARAMETERS = {
    "source_commit": SOURCE_COMMIT,
    "poll_minutes": 5,
    "cpr_thresholds_pct": {"narrow_below": 0.25, "wide_above": 0.75},
    "oi_confirmation": "strict_sign_from_live_calculate_oi_metrics",
    "target_r_multiple": 1.5,
    "risk_per_trade_bps": 50.0,
    "max_open_positions": 4,
    "max_aggregate_open_risk_inr": 1000.0,
    "max_gross_notional_inr": 200000.0,
    "hard_daily_loss_inr": 1000.0,
    "roll_rule": "next_month_from_start_of_expiry_session",
    "split": [0.60, 0.20, 0.20],
    "bootstrap_seed": BOOTSTRAP_SEED,
    "bootstrap_resamples": 10000,
    "live_function_sha256": {
        "calculate_cpr": "855b685234b6fa49eb6691f004cadcaea3dbec25fab39737acb7d2e5244b1ad7",
        "calculate_oi_metrics": "c6154ca84fb21f219b80c9be36c1f18a1d4340ed73ec1603dcc95239a1e0079e",
        "DualEngine._generate_opinion": "35e953a2174c19d2695a83818cfa42938f524bb8ca22cabd36728864ff24caf2",
    },
}


def chunks(start: date, end: date):
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=27), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def load_exact_live(dualengine: Path):
    import subprocess
    commit = subprocess.check_output(
        ["git", "-C", str(dualengine), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != SOURCE_COMMIT:
        raise RuntimeError(f"DualEngine must be locked at {SOURCE_COMMIT}; found {commit}")
    sys.path.insert(0, str(dualengine))
    from engine.engine_core import DualEngine
    from market_data import FuturesSnapshot, OHLCSnapshot
    from cpr import calculate_cpr
    from oi_intelligence import calculate_oi_metrics
    functions = {
        "calculate_cpr": calculate_cpr,
        "calculate_oi_metrics": calculate_oi_metrics,
        "DualEngine._generate_opinion": DualEngine._generate_opinion,
    }
    actual = {
        name: hashlib.sha256(inspect.getsource(function).encode()).hexdigest()
        for name, function in functions.items()
    }
    if actual != LOCKED_PARAMETERS["live_function_sha256"]:
        raise RuntimeError(f"live strategy function hash mismatch: {actual}")
    return DualEngine, FuturesSnapshot, OHLCSnapshot


def fetch_inputs(pipeline, cash, futures, selected, start, end, workers):
    cash_ids = sorted({row.isin for rows in cash.values() for row in rows.values() if row.isin and row.symbol in futures.get(row.session_date, {})})
    futures_ranges = {}
    for session, symbols in selected.items():
        for choice in symbols.values():
            key = (choice.bar.instrument_id, choice.bar.expiry)
            low, high = futures_ranges.get(key, (session, session))
            futures_ranges[key] = (min(low, session - timedelta(days=7)), max(high, session))

    db_path = pipeline.cache_dir / "bars.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    conn.execute("DROP TABLE IF EXISTS bars")
    conn.execute("""
        CREATE TABLE bars (
            kind VARCHAR, instrument VARCHAR, timestamp VARCHAR,
            session_date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume BIGINT, oi BIGINT
        )
    """)
    spool_path = pipeline.cache_dir / "bars.csv"
    spool = spool_path.open("w", encoding="utf-8", newline="")
    writer = csv.writer(spool)
    specs = []
    for isin in cash_ids:
        for low, high in chunks(start - timedelta(days=7), end):
            specs.append(("cash", isin, f"NSE_EQ|{isin}", low, high, False))
    today = datetime.now(timezone.utc).date()
    for (instrument_id, expiry), (low, high) in sorted(futures_ranges.items(), key=str):
        expired = expiry < today
        key = f"NSE_FO|{instrument_id}|{expiry:%d-%m-%Y}" if expired else f"NSE_FO|{instrument_id}"
        specs.append(("future", f"{instrument_id}|{expiry}", key, low, high, expired))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        spec_iter = iter(specs)
        for spec in spec_iter:
            kind, stored_key, key, low, high, expired = spec
            task = pool.submit(pipeline.candles, key, low, high, expired=expired)
            pending[task] = (kind, stored_key, low, high)
            if len(pending) >= workers * 2:
                break
        complete = 0
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for task in done:
                kind, key, low, high = pending.pop(task)
                try:
                    rows = task.result()
                except Exception as exc:
                    pipeline.hasher.add(
                        f"failed-{kind}|{key}|{low}|{high}",
                        f"{type(exc).__name__}:{exc}".encode(),
                    )
                    cursor = low
                    while cursor <= high:
                        pipeline._exclude(cursor, key, f"UPSTOX_FETCH_FAILED:{type(exc).__name__}")
                        cursor += timedelta(days=1)
                    print(
                        f"data exclusion {kind} {key} {low}..{high}: "
                        f"{type(exc).__name__} {exc}",
                        flush=True,
                    )
                    rows = []
                writer.writerows(
                    (
                        kind, key, row.timestamp, row.timestamp.astimezone(IST).date(),
                        row.open, row.high, row.low, row.close, row.volume, row.oi,
                    ) for row in rows
                )
                complete += 1
                if complete % 100 == 0:
                    print(f"data fetch {complete}/{len(specs)}", flush=True)
                try:
                    spec = next(spec_iter)
                except StopIteration:
                    continue
                next_kind, stored_key, candle_key, low, high, expired = spec
                next_task = pool.submit(pipeline.candles, candle_key, low, high, expired=expired)
                pending[next_task] = (next_kind, stored_key, low, high)
        action_tasks = {isin: pool.submit(pipeline.corporate_actions, isin) for isin in cash_ids}
        actions = {}
        for isin, task in action_tasks.items():
            try:
                actions[isin] = task.result()
            except Exception as exc:
                pipeline.hasher.add(
                    f"failed-corporate-actions|{isin}",
                    f"{type(exc).__name__}:{exc}".encode(),
                )
                print(f"data exclusion actions {isin}: {type(exc).__name__} {exc}", flush=True)
    spool.close()
    escaped_spool = str(spool_path).replace("'", "''")
    conn.execute(f"COPY bars FROM '{escaped_spool}' (FORMAT CSV)")
    conn.close()
    return db_path, actions


def load_session_bars(conn, session):
    grouped = {"cash": {}, "future": {}}
    rows = conn.execute(
        "SELECT kind, instrument, timestamp, open, high, low, close, volume, oi FROM bars WHERE session_date = ? ORDER BY timestamp",
        [session],
    ).fetchall()
    for kind, instrument, timestamp, open_p, high, low, close, volume, oi in rows:
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        grouped[kind].setdefault(instrument, []).append(
            Bar(timestamp, open_p, high, low, close, volume, oi)
        )
    return grouped


def prior_contract_bar(futures, sessions, session, symbol, selected_bar):
    for previous in reversed([value for value in sessions if value < session]):
        for row in futures[previous].get(symbol, []):
            if row.instrument_id == selected_bar.instrument_id and row.expiry == selected_bar.expiry:
                return row
        if (session - previous).days > 10:
            break
    return None


def simulate_symbol_day(
    *, pipeline, session, symbol, cash_daily, future_daily, cohort,
    cash_rows, future_rows, actions, end, engine, snapshot_types,
):
    FuturesSnapshot, OHLCSnapshot = snapshot_types
    cash_day = [x for x in cash_rows if x.timestamp.astimezone(IST).date() == session and time(9, 15) <= x.timestamp.astimezone(IST).time() <= time(15, 30)]
    future_by_ts = {x.timestamp: x for x in future_rows if x.timestamp.astimezone(IST).date() == session}
    if len(cash_day) < 2:
        return []
    adjusted = lambda value, day: pipeline.adjusted(value, day, actions, end)
    previous_ohlc = OHLCSnapshot(
        symbol=symbol, instrument_key=f"NSE_EQ|{cash_daily.isin}", as_of=session.isoformat(),
        open=adjusted(cash_daily.open, cash_daily.session_date), high=adjusted(cash_daily.high, cash_daily.session_date),
        low=adjusted(cash_daily.low, cash_daily.session_date), close=adjusted(cash_daily.close, cash_daily.session_date),
        volume=cash_daily.volume, prev_close=adjusted(cash_daily.close, cash_daily.session_date), last_price=1.0,
    )
    cost_model = IndiaEquityIntradayCostModel()
    risk_desk = RiskDesk(capital=STARTING_CAPITAL, risk_per_trade_bps=50.0)
    result = []
    index = 0
    while index < len(cash_day) - 1:
        equity = cash_day[index]
        future = future_by_ts.get(equity.timestamp)
        if future is None or index > 0 and has_gap(cash_day, index - 1, index):
            index += 1
            continue
        current_price = adjusted(equity.close, session)
        snap = OHLCSnapshot(**{**previous_ohlc.__dict__, "last_price": current_price}) if hasattr(previous_ohlc, "__dict__") else OHLCSnapshot(
            symbol=symbol, instrument_key=previous_ohlc.instrument_key, as_of=previous_ohlc.as_of,
            open=previous_ohlc.open, high=previous_ohlc.high, low=previous_ohlc.low, close=previous_ohlc.close,
            volume=previous_ohlc.volume, prev_close=previous_ohlc.prev_close, last_price=current_price,
        )
        future_snap = FuturesSnapshot(
            symbol=symbol, instrument_key=f"NSE_FO|{future_daily.instrument_id}", as_of=future.timestamp.isoformat(),
            last_price=future.close, prev_close=future_daily.close, current_oi=future.oi,
            prev_oi=future_daily.oi, oi_day_high=None, oi_day_low=None,
        )
        record = engine.evaluate_stock(symbol, snap, future_snap, now=equity.timestamp)
        if record.dualengine_direction not in ("LONG", "SHORT") or record.confidence < 0.75:
            index += 1
            continue
        direction = Direction.LONG if record.dualengine_direction == "LONG" else Direction.SHORT
        stop = record.factual_metrics["cpr_bc"] if direction == Direction.LONG else record.factual_metrics["cpr_tc"]
        slip = liquidity_slippage_bps_per_side(cash_daily.close * cash_daily.volume)
        signal = SignalCandidate(
            signal_id=f"{session}-{symbol}-{index}", instrument_id=symbol, created_at=equity.timestamp,
            valid_until=equity.timestamp + timedelta(minutes=5), direction=direction,
            setup_type="CPR_OI_INTELLIGENCE", regime=MarketRegime.TRENDING,
            confidence=record.confidence, entry_trigger=current_price, invalidation_level=stop,
            reason_codes=record.reason_codes, slippage_bps_per_side=slip, cohort=cohort,
        )
        evaluation = risk_desk.evaluate(signal)
        if not evaluation.approved:
            index += 1
            continue
        exit_index = None
        exit_price = None
        for probe in range(index + 1, len(cash_day)):
            if has_gap(cash_day, index, probe):
                break
            bar = cash_day[probe]
            low = adjusted(bar.low, session); high = adjusted(bar.high, session)
            if direction == Direction.LONG:
                if low <= evaluation.stop_loss:
                    exit_price = evaluation.stop_loss; exit_index = probe; break
                if high >= evaluation.target:
                    exit_price = evaluation.target; exit_index = probe; break
            else:
                if high >= evaluation.stop_loss:
                    exit_price = evaluation.stop_loss; exit_index = probe; break
                if low <= evaluation.target:
                    exit_price = evaluation.target; exit_index = probe; break
            if bar.timestamp.astimezone(IST).time() >= time(15, 15):
                exit_price = adjusted(bar.close, session); exit_index = probe; break
        if exit_index is None or exit_price is None:
            pipeline._exclude(session, symbol, "TRADE_TOUCHED_BAR_GAP")
            index += 1
            continue
        gross = (exit_price - current_price) * evaluation.quantity * (1 if direction == Direction.LONG else -1)
        friction = cost_model.estimate_trade_friction(
            entry_price=Decimal(str(current_price)), exit_price=Decimal(str(exit_price)),
            quantity=evaluation.quantity, slippage_bps_per_side=slip,
        )
        net = gross - float(friction.total)
        result.append(Trade(session, symbol, cohort, equity.timestamp, cash_day[exit_index].timestamp, direction.value, evaluation.quantity, current_price, exit_price, net, net, abs(current_price - evaluation.stop_loss) * evaluation.quantity))
        index = exit_index + 1
    return result


def portfolio_filter(candidates):
    accepted = []
    for trade in sorted(candidates, key=lambda x: (x.opened_at, x.symbol)):
        active = [x for x in accepted if x.opened_at.date() == trade.opened_at.date() and x.closed_at > trade.opened_at]
        risk = trade.risk_inr
        if len(active) >= 4 or sum(x.risk_inr for x in active) + risk > 1000.0:
            continue
        if sum(x.entry * x.quantity for x in active) + trade.entry * trade.quantity > 200000.0:
            continue
        realised_day = sum(x.net_pnl for x in accepted if x.session_date == trade.session_date and x.closed_at <= trade.opened_at)
        if realised_day <= -1000.0:
            continue
        accepted.append(trade)
    return accepted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", type=date.fromisoformat, default=date(2025, 9, 1))
    parser.add_argument("--to-date", type=date.fromisoformat, default=date(2026, 9, 7))
    parser.add_argument("--dualengine", type=Path, default=ROOT.parent / "dualengine")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data" / "reconciled_cpr_oi")
    parser.add_argument("--output", type=Path, default=ROOT / "research" / "evidence" / "reconciled_cpr_oi_backtest.json")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    golden_rules = ROOT / "goldenrules.txt"
    if not golden_rules.is_file() or not golden_rules.read_bytes().strip():
        raise RuntimeError("mandatory goldenrules.txt is missing or empty")
    DualEngine, FuturesSnapshot, OHLCSnapshot = load_exact_live(args.dualengine)
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    pipeline = ReconciledDataPipeline(token, args.cache_dir)
    cash, futures = pipeline.nse_sessions(args.from_date, args.to_date)
    selected = pipeline.select_contracts(futures)
    bars_db, actions = fetch_inputs(pipeline, cash, futures, selected, args.from_date, args.to_date, args.workers)
    bars_conn = duckdb.connect(str(bars_db), read_only=True)
    sessions = sorted(cash)
    candidates = []
    for number, session in enumerate(sessions, 1):
        previous = sessions[sessions.index(session) - 1] if sessions.index(session) else None
        if previous is None:
            continue
        session_bars = load_session_bars(bars_conn, session)
        previous_bars = load_session_bars(bars_conn, previous)
        for symbol, choice in selected[session].items():
            current_cash = cash[session].get(symbol); prior_cash = cash[previous].get(symbol)
            if current_cash is None or prior_cash is None or not current_cash.isin:
                pipeline._exclude(session, symbol, "CASH_ELIGIBILITY_MAPPING_MISSING"); continue
            if current_cash.isin not in actions:
                pipeline._exclude(session, symbol, "CORPORATE_ACTION_DATA_MISSING"); continue
            prior_future = prior_contract_bar(futures, sessions, session, symbol, choice.bar)
            if prior_future is None:
                pipeline._exclude(session, symbol, "PRIOR_SAME_CONTRACT_MISSING"); continue
            eq_rows = session_bars["cash"].get(current_cash.isin, [])
            prior_eq_rows = previous_bars["cash"].get(current_cash.isin, [])
            fut_key = (choice.bar.instrument_id, choice.bar.expiry)
            stored_fut_key = f"{choice.bar.instrument_id}|{choice.bar.expiry}"
            fut_rows = session_bars["future"].get(stored_fut_key, [])
            prior_fut_rows = previous_bars["future"].get(stored_fut_key, [])
            if (not pipeline.reconcile(current_cash, eq_rows, futures=False)
                    or not pipeline.reconcile(prior_cash, prior_eq_rows, futures=False)
                    or not pipeline.reconcile(choice.bar, fut_rows, futures=True)
                    or not pipeline.reconcile(prior_future, prior_fut_rows, futures=True)):
                continue
            # CPR is prior cash; OI comparison is prior day of the exact selected contract.
            rows = simulate_symbol_day(
                pipeline=pipeline, session=session, symbol=symbol, cash_daily=prior_cash,
                future_daily=prior_future, cohort=choice.cohort, cash_rows=eq_rows,
                future_rows=fut_rows, actions=actions[current_cash.isin], end=args.to_date,
                engine=DualEngine(), snapshot_types=(FuturesSnapshot, OHLCSnapshot),
            )
            candidates.extend(rows)
        if number % 20 == 0:
            print(f"backtest {number}/{len(sessions)} sessions, {len(candidates)} candidates", flush=True)
    bars_conn.close()
    trades = portfolio_filter(candidates)
    partitions = split_dates(sessions)
    results = {name: metrics([x for x in trades if x.session_date in dates], starting_capital=STARTING_CAPITAL, seed=BOOTSTRAP_SEED) for name, dates in partitions.items()}
    results["overall"] = metrics(trades, starting_capital=STARTING_CAPITAL, seed=BOOTSTRAP_SEED)
    cohorts = {name: metrics([x for x in trades if x.cohort == name], starting_capital=STARTING_CAPITAL, seed=BOOTSTRAP_SEED) for name in sorted({x.cohort for x in trades})}
    dataset_hash = canonical_json_hash({"inputs": pipeline.hasher.hexdigest, "parameters": LOCKED_PARAMETERS})
    holdout = results["holdout"]
    missed = []
    if holdout["trades"] < 400: missed.append("holdout_trades<400")
    if holdout["days"] < 60: missed.append("holdout_days<60")
    if holdout["profit_factor"] < 1.25: missed.append("holdout_profit_factor<1.25")
    if holdout["max_drawdown_pct"] > 15.0: missed.append("holdout_max_drawdown>15%")
    if holdout["expectancy_ci_95_inr"][0] is None or holdout["expectancy_ci_95_inr"][0] <= 0: missed.append("holdout_expectancy_ci_lower<=0")
    report = {
        "dataset_hash": dataset_hash, "bootstrap_seed": BOOTSTRAP_SEED,
        "locked_parameters": LOCKED_PARAMETERS, "results": results, "cohorts": cohorts,
        "verdict": "PASS" if not missed else "FAIL", "missed_gates": missed,
        "integrity_exclusions": pipeline.exclusions,
        "trades": [{**asdict(x), "session_date": x.session_date.isoformat(), "opened_at": x.opened_at.isoformat(), "closed_at": x.closed_at.isoformat()} for x in trades],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("dataset_hash", "bootstrap_seed", "results", "verdict", "missed_gates")}, indent=2))


if __name__ == "__main__":
    main()
