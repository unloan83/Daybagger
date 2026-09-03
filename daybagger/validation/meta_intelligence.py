from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean, median
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from daybagger.bootstrap import verify_golden_rules
from daybagger.config import load_settings
from daybagger.data.upstox import IntradayCandle, UpstoxMarketData
from daybagger.decision.model import ValidatedLinearModel, ValidatedModelSpec
from daybagger.domain import DecisionStatus, Direction, Opportunity
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.decision.risk import AdaptiveCapitalAllocator, CapitalState
from daybagger.intelligence.meta_features import (
    MetaFeatureError,
    build_cross_section_state,
    build_meta_raw_features,
)
from daybagger.intelligence.upstox_external import lagged_institutional_features
from daybagger.meta.forest import export_random_forest_regressor
from daybagger.meta.stack import (
    BASE_FAMILIES,
    MetaIntelligenceSpec,
    build_meta_features,
    choose_meta_feature_names,
    promote_meta_spec,
)
from daybagger.specialists.catalog import SPECIALIST_FAMILIES
from daybagger.specialists.trainer import TrainingRow, fit_logistic_specialist
from daybagger.validation.historical import HistoricalCandleClient
from daybagger.validation.metrics import PredictionOutcome, ValidationMetrics, evaluate_predictions
from daybagger.validation.registry import ModelRegistry


INDIA = ZoneInfo("Asia/Kolkata")
NIFTY_KEY = "NSE_INDEX|Nifty 50"
BANK_NIFTY_KEY = "NSE_INDEX|Nifty Bank"
INDIA_VIX_KEY = "NSE_INDEX|India VIX"
FIXED_HORIZONS: tuple[int, ...] = (15, 30, 60)


@dataclass(frozen=True, slots=True)
class MetaSample:
    session_date: date
    symbol: str
    as_of: datetime
    raw_features: Mapping[str, float]
    entry_price: Decimal
    long_gross_return_bps: float
    short_gross_return_bps: float
    long_net_return_bps: float
    short_net_return_bps: float

    def gross(self, direction: Direction) -> float:
        if direction == Direction.LONG:
            return self.long_gross_return_bps
        if direction == Direction.SHORT:
            return self.short_gross_return_bps
        raise ValueError("FLAT has no realised return")

    def net(self, direction: Direction) -> float:
        if direction == Direction.LONG:
            return self.long_net_return_bps
        if direction == Direction.SHORT:
            return self.short_net_return_bps
        raise ValueError("FLAT has no realised return")


@dataclass(frozen=True, slots=True)
class MetaTrainingRow:
    sample: MetaSample
    meta_features: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class PortfolioEvidence:
    metrics: ValidationMetrics
    selected_sessions: int
    total_net_return_bps: float
    rank_ic: float
    session_ci95_low_bps: float
    session_ci95_high_bps: float
    baseline_brier: float


@dataclass(frozen=True, slots=True)
class HorizonEvidence:
    horizon_minutes: int
    meta_feature_names: tuple[str, ...]
    oof_rows: int
    meta_train_sessions: int
    development_eval: PortfolioEvidence


@dataclass(frozen=True, slots=True)
class MetaValidationResult:
    approved: bool
    validation_id: str
    selected_horizon_minutes: int
    development: PortfolioEvidence
    holdout: PortfolioEvidence
    usable_symbols: tuple[str, ...]
    meta_feature_names: tuple[str, ...]
    reason: str
    approved_path: str | None
    evidence_path: str


def group_sessions(candles: Sequence[IntradayCandle]) -> dict[date, list[IntradayCandle]]:
    result: dict[date, list[IntradayCandle]] = {}
    for candle in sorted(candles, key=lambda c: c.timestamp):
        local_date = candle.timestamp.astimezone(INDIA).date()
        result.setdefault(local_date, []).append(candle)
    return result


def build_meta_samples(
    *,
    stock_sessions_by_symbol: Mapping[str, Mapping[date, Sequence[IntradayCandle]]],
    market_sessions: Mapping[date, Sequence[IntradayCandle]],
    bank_sessions: Mapping[date, Sequence[IntradayCandle]],
    vix_sessions: Mapping[date, Sequence[IntradayCandle]],
    sector_by_symbol: Mapping[str, str],
    institutional_history: Mapping[date, Mapping[str, float]] | None,
    horizon_minutes: int,
    round_trip_cost_bps: float,
    warmup_bars: int = 30,
    stride_bars: int = 15,
    volume_lookback_sessions: int = 20,
) -> list[MetaSample]:
    if horizon_minutes <= 0 or warmup_bars < 30 or stride_bars <= 0:
        raise ValueError("invalid meta sample parameters")
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps cannot be negative")
    if volume_lookback_sessions < 5:
        raise ValueError("volume_lookback_sessions must be >= 5")

    symbols = sorted(set(stock_sessions_by_symbol).intersection(sector_by_symbol))
    if len(symbols) < 6:
        raise RuntimeError("meta intelligence needs at least six sector-mapped equities")

    # Context dates define the session calendar. Individual equities are allowed to
    # be absent on a session; the cross-section uses only genuine aligned symbols.
    common_dates = set(market_sessions).intersection(bank_sessions).intersection(vix_sessions)

    result: list[MetaSample] = []
    for session_date in sorted(common_dates):
        market = sorted(market_sessions.get(session_date, ()), key=lambda c: c.timestamp)
        bank = sorted(bank_sessions.get(session_date, ()), key=lambda c: c.timestamp)
        vix = sorted(vix_sessions.get(session_date, ()), key=lambda c: c.timestamp)
        if min(len(market), len(bank), len(vix)) < warmup_bars:
            continue

        market_by_ts = {c.timestamp: i for i, c in enumerate(market)}
        bank_by_ts = {c.timestamp: i for i, c in enumerate(bank)}
        vix_by_ts = {c.timestamp: i for i, c in enumerate(vix)}
        decision_times = [
            market[i].timestamp
            for i in range(warmup_bars - 1, len(market), stride_bars)
        ]

        stock_for_day: dict[str, list[IntradayCandle]] = {
            symbol: sorted(
                stock_sessions_by_symbol[symbol].get(session_date, ()),
                key=lambda c: c.timestamp,
            )
            for symbol in symbols
        }
        stock_index: dict[str, dict[datetime, int]] = {
            symbol: {c.timestamp: i for i, c in enumerate(candles)}
            for symbol, candles in stock_for_day.items()
            if candles
        }

        external = (
            lagged_institutional_features(institutional_history, session_date)
            if institutional_history
            else None
        )

        for as_of in decision_times:
            if as_of not in bank_by_ts or as_of not in vix_by_ts:
                continue
            prefixes: dict[str, Sequence[IntradayCandle]] = {}
            for symbol, candles in stock_for_day.items():
                idx = stock_index.get(symbol, {}).get(as_of)
                if idx is not None and idx + 1 >= warmup_bars:
                    prefixes[symbol] = candles[: idx + 1]
            if len(prefixes) < 6:
                continue
            try:
                cross = build_cross_section_state(
                    session_date=session_date,
                    as_of=as_of,
                    prefixes_by_symbol=prefixes,
                    sector_by_symbol=sector_by_symbol,
                )
            except MetaFeatureError:
                continue

            market_prefix = market[: market_by_ts[as_of] + 1]
            bank_prefix = bank[: bank_by_ts[as_of] + 1]
            vix_prefix = vix[: vix_by_ts[as_of] + 1]

            for symbol, stock_prefix in prefixes.items():
                ordered_dates = [
                    d
                    for d in sorted(stock_sessions_by_symbol[symbol])
                    if d < session_date
                ][-volume_lookback_sessions:]
                prior = [stock_sessions_by_symbol[symbol][d] for d in ordered_dates]
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
                        sector=sector_by_symbol[symbol],
                        prior_stock_sessions=prior,
                        external_numeric=external,
                    )
                except Exception:
                    continue

                candles = stock_for_day[symbol]
                idx = stock_index[symbol][as_of]
                entry_index = idx + 1
                if entry_index >= len(candles):
                    continue
                exit_ts = as_of + timedelta(minutes=horizon_minutes)
                exit_index = stock_index[symbol].get(exit_ts)
                if exit_index is None or exit_index < entry_index:
                    continue
                if exit_ts.astimezone(INDIA).time() > time(15, 10):
                    continue

                entry_price = candles[entry_index].open
                stop_bps = float(raw["stock_session_range_bps"])
                if entry_price <= 0 or stop_bps <= 0:
                    continue
                window = candles[entry_index : exit_index + 1]
                long_gross = _gross_with_range_stop(
                    entry_price=entry_price,
                    exit_price=candles[exit_index].close,
                    bars=window,
                    direction=Direction.LONG,
                    stop_bps=stop_bps,
                )
                short_gross = _gross_with_range_stop(
                    entry_price=entry_price,
                    exit_price=candles[exit_index].close,
                    bars=window,
                    direction=Direction.SHORT,
                    stop_bps=stop_bps,
                )
                result.append(
                    MetaSample(
                        session_date=session_date,
                        symbol=symbol,
                        as_of=as_of,
                        raw_features=raw,
                        entry_price=entry_price,
                        long_gross_return_bps=long_gross,
                        short_gross_return_bps=short_gross,
                        long_net_return_bps=long_gross - round_trip_cost_bps,
                        short_net_return_bps=short_gross - round_trip_cost_bps,
                    )
                )
    return result


def validate_meta_intelligence(
    *,
    repo_root: Path,
    access_token: str,
    symbol_to_instrument: Mapping[str, str],
    sector_by_symbol: Mapping[str, str],
    institutional_history: Mapping[date, Mapping[str, float]] | None,
    from_date: date,
    to_date: date,
    horizons: Sequence[int] = FIXED_HORIZONS,
    final_holdout_sessions: int = 15,
    validation_notional_inr: Decimal = Decimal("30000"),
    approved_path: Path | None = None,
    evidence_dir: Path | None = None,
    registry_path: Path | None = None,
) -> MetaValidationResult:
    repo_root = repo_root.resolve()
    verify_golden_rules(repo_root)
    if not access_token.strip():
        raise ValueError("access_token is required")
    if from_date >= to_date:
        raise ValueError("from_date must be before to_date")
    if final_holdout_sessions < 10:
        raise ValueError("final_holdout_sessions must be at least 10")
    fixed = tuple(int(h) for h in horizons)
    if fixed != FIXED_HORIZONS:
        raise ValueError(
            f"validation horizons are locked at {FIXED_HORIZONS}; post-result tuning is forbidden"
        )
    if len(symbol_to_instrument) < 6:
        raise ValueError("validation universe must contain at least six equities")
    if set(symbol_to_instrument) - set(sector_by_symbol):
        missing = sorted(set(symbol_to_instrument) - set(sector_by_symbol))
        raise ValueError(f"sector mapping missing for: {missing}")

    approved_path = approved_path or repo_root / "config" / "validated_meta_model.json"
    evidence_dir = evidence_dir or repo_root / "research" / "evidence" / "meta_intelligence"
    registry_path = registry_path or repo_root / "research" / "model_registry.json"

    market_data = UpstoxMarketData(access_token=access_token)
    historical = HistoricalCandleClient(market_data)
    contexts = {
        "market": group_sessions(historical.fetch(NIFTY_KEY, from_date=from_date, to_date=to_date)),
        "bank": group_sessions(historical.fetch(BANK_NIFTY_KEY, from_date=from_date, to_date=to_date)),
        "vix": group_sessions(historical.fetch(INDIA_VIX_KEY, from_date=from_date, to_date=to_date)),
    }
    stocks: dict[str, Mapping[date, Sequence[IntradayCandle]]] = {}
    for symbol, key in symbol_to_instrument.items():
        candles = historical.fetch(key, from_date=from_date, to_date=to_date)
        grouped = group_sessions(candles)
        if grouped:
            stocks[symbol] = grouped
    usable = tuple(sorted(stocks))
    if len(usable) < 6:
        raise RuntimeError("fewer than six equities returned usable historical candles")
    sectors = {symbol: sector_by_symbol[symbol] for symbol in usable}
    # Locked unseen-symbol cohort. Selection is deterministic and declared before
    # model fitting; these symbols never contribute to development training.
    symbol_holdout = tuple(symbol for idx, symbol in enumerate(usable) if idx % 5 == 0)
    development_symbols = set(usable) - set(symbol_holdout)
    if len(symbol_holdout) < 2 or len(development_symbols) < 6:
        raise RuntimeError("validation universe is too small for locked symbol holdout")

    settings = load_settings(repo_root / "config" / "default.toml")
    cost_model = IndiaEquityIntradayCostModel()
    # Historical order-book spread is unavailable and is never fabricated. Known
    # statutory/brokerage cost is computed at the declared validation notional.
    # Paper slippage is a declared execution assumption and is charged on BOTH
    # entry and exit so historical validation cannot be more optimistic than the
    # canonical paper runtime merely because historical bid/ask is unavailable.
    statutory_cost_bps = cost_model.round_trip_bps_for_notional(validation_notional_inr)
    execution_allowance_bps = 2.0 * settings.execution.paper_slippage_bps
    cost_bps = statutory_cost_bps + execution_allowance_bps

    samples_by_horizon: dict[int, list[MetaSample]] = {}
    all_dates: set[date] = set()
    for horizon in FIXED_HORIZONS:
        samples = build_meta_samples(
            stock_sessions_by_symbol=stocks,
            market_sessions=contexts["market"],
            bank_sessions=contexts["bank"],
            vix_sessions=contexts["vix"],
            sector_by_symbol=sectors,
            institutional_history=institutional_history,
            horizon_minutes=horizon,
            round_trip_cost_bps=cost_bps,
        )
        samples_by_horizon[horizon] = samples
        all_dates.update(sample.session_date for sample in samples)

    ordered_dates = sorted(all_dates)
    if len(ordered_dates) < 55:
        raise RuntimeError(
            f"insufficient aligned sessions for locked meta protocol: {len(ordered_dates)}; need >=55"
        )
    holdout_dates = set(ordered_dates[-final_holdout_sessions:])
    development_dates = ordered_dates[:-final_holdout_sessions]
    if len(development_dates) < 40:
        raise RuntimeError("development period must contain at least 40 sessions")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    validation_id = f"meta-v4-direct-return-{from_date.isoformat()}-{to_date.isoformat()}-{stamp}"

    horizon_evidence: list[HorizonEvidence] = []
    horizon_oof: dict[int, list[MetaTrainingRow]] = {}
    for horizon in FIXED_HORIZONS:
        development = [
            sample for sample in samples_by_horizon[horizon]
            if sample.session_date in set(development_dates)
            and sample.symbol in development_symbols
        ]
        oof = _build_expanding_oof_rows(
            samples=development,
            development_dates=development_dates,
            horizon_minutes=horizon,
            validation_id=validation_id,
        )
        if not oof:
            continue
        oof_dates = sorted({row.sample.session_date for row in oof})
        if len(oof_dates) < 15:
            continue
        eval_sessions = max(5, len(oof_dates) // 4)
        meta_eval_dates = set(oof_dates[-eval_sessions:])
        meta_train_rows = [row for row in oof if row.sample.session_date not in meta_eval_dates]
        meta_eval_rows = [row for row in oof if row.sample.session_date in meta_eval_dates]
        if not meta_train_rows or not meta_eval_rows:
            continue
        feature_names = choose_meta_feature_names([row.meta_features for row in meta_train_rows])
        long_model, short_model = _fit_meta_regressors(
            rows=meta_train_rows,
            feature_names=feature_names,
            horizon_minutes=horizon,
            validation_id=validation_id,
        )
        evidence = _evaluate_portfolio(
            rows=meta_eval_rows,
            long_model=long_model,
            short_model=short_model,
            feature_names=feature_names,
            cost_bps=cost_bps,
            horizon_minutes=horizon,
            starting_capital_inr=Decimal(str(settings.capital.starting_capital_inr)),
            max_risk_per_trade_inr=Decimal(str(settings.risk.max_risk_per_trade_inr)),
            hard_daily_loss_limit_inr=Decimal(str(settings.risk.hard_daily_loss_limit_inr)),
            max_aggregate_open_risk_inr=Decimal(str(settings.risk.max_aggregate_open_risk_inr)),
            max_position_fraction=settings.risk.max_position_fraction,
        )
        horizon_evidence.append(
            HorizonEvidence(
                horizon_minutes=horizon,
                meta_feature_names=feature_names,
                oof_rows=len(oof),
                meta_train_sessions=len({r.sample.session_date for r in meta_train_rows}),
                development_eval=evidence,
            )
        )
        horizon_oof[horizon] = oof

    if not horizon_evidence:
        raise RuntimeError("locked development protocol produced no trainable meta horizon")

    # Horizon selection happens ONLY on development OOS evidence. Holdout remains unopened.
    selected_dev = max(
        horizon_evidence,
        key=lambda item: (
            item.development_eval.total_net_return_bps,
            item.development_eval.rank_ic,
            -item.development_eval.metrics.max_drawdown_bps,
        ),
    )
    horizon = selected_dev.horizon_minutes
    selected_samples = samples_by_horizon[horizon]
    development = [
        s for s in selected_samples
        if s.session_date in set(development_dates) and s.symbol in development_symbols
    ]
    holdout = [
        s for s in selected_samples
        if s.session_date in holdout_dates and s.symbol in set(symbol_holdout)
    ]

    base_specs = _fit_base_specs(
        samples=development,
        horizon_minutes=horizon,
        validation_id=validation_id,
    )
    full_oof = horizon_oof[horizon]
    feature_names = choose_meta_feature_names([row.meta_features for row in full_oof])
    long_model, short_model = _fit_meta_regressors(
        rows=full_oof,
        feature_names=feature_names,
        horizon_minutes=horizon,
        validation_id=validation_id,
    )

    holdout_rows = [
        MetaTrainingRow(
            sample=sample,
            meta_features=build_meta_features(
                symbol=sample.symbol,
                as_of=sample.as_of,
                raw_features=sample.raw_features,
                base_specs=base_specs,
            ),
        )
        for sample in holdout
    ]
    holdout_evidence = _evaluate_portfolio(
        rows=holdout_rows,
        long_model=long_model,
        short_model=short_model,
        feature_names=feature_names,
        cost_bps=cost_bps,
        horizon_minutes=horizon,
        starting_capital_inr=Decimal(str(settings.capital.starting_capital_inr)),
        max_risk_per_trade_inr=Decimal(str(settings.risk.max_risk_per_trade_inr)),
        hard_daily_loss_limit_inr=Decimal(str(settings.risk.hard_daily_loss_limit_inr)),
        max_aggregate_open_risk_inr=Decimal(str(settings.risk.max_aggregate_open_risk_inr)),
        max_position_fraction=settings.risk.max_position_fraction,
    )

    reasons: list[str] = []
    if selected_dev.development_eval.metrics.avg_net_return_bps <= 0:
        reasons.append("development_net_expectancy_non_positive")
    if (selected_dev.development_eval.metrics.profit_factor or 0.0) <= 1.0:
        reasons.append("development_profit_factor_not_above_one")
    if selected_dev.development_eval.rank_ic <= 0:
        reasons.append("development_cross_section_rank_ic_non_positive")
    if selected_dev.development_eval.metrics.brier_score >= selected_dev.development_eval.baseline_brier:
        reasons.append("development_calibration_not_better_than_base_rate")
    if holdout_evidence.metrics.avg_net_return_bps <= 0:
        reasons.append("holdout_net_expectancy_non_positive")
    if (holdout_evidence.metrics.profit_factor or 0.0) <= 1.0:
        reasons.append("holdout_profit_factor_not_above_one")
    if holdout_evidence.rank_ic <= 0:
        reasons.append("holdout_cross_section_rank_ic_non_positive")
    if holdout_evidence.metrics.brier_score >= holdout_evidence.baseline_brier:
        reasons.append("holdout_calibration_not_better_than_base_rate")
    if holdout_evidence.metrics.observations < 8 or holdout_evidence.selected_sessions < 5:
        reasons.append("holdout_insufficient_selected_opportunities")
    approved = not reasons

    evidence_summary = {
        "method": "LOCKED_DIRECT_RETURN_CROSS_SECTION_META_V4",
        "horizons_considered": list(FIXED_HORIZONS),
        "selected_horizon_minutes": horizon,
        "historical_spread_policy": "NOT_INVENTED; live spread required at execution",
        "historical_execution_policy": "STATUTORY_COST_PLUS_DECLARED_TWO_SIDED_PAPER_SLIPPAGE; NO_SYNTHETIC_SPREAD",
        "stop_policy": "decision-time observed session range; same in historical and live paper",
        "cost_bps_known_statutory": statutory_cost_bps,
        "cost_bps_execution_allowance": execution_allowance_bps,
        "cost_bps_validation_total_excluding_unknown_historical_spread": cost_bps,
        "development": _portfolio_to_dict(selected_dev.development_eval),
        "holdout": _portfolio_to_dict(holdout_evidence),
        "horizon_development_evidence": [
            {
                "horizon_minutes": item.horizon_minutes,
                "oof_rows": item.oof_rows,
                "meta_train_sessions": item.meta_train_sessions,
                "development_eval": _portfolio_to_dict(item.development_eval),
            }
            for item in horizon_evidence
        ],
        "research_universe_symbols": list(usable),
        "development_symbols": sorted(development_symbols),
        "unseen_symbol_holdout": list(symbol_holdout),
        "holdout_policy": "final 15 sessions AND unseen deterministic 20% symbol cohort",
        "sector_by_symbol": {symbol: sectors[symbol] for symbol in usable},
        "institutional_features_used": any(
            name.startswith(("fii_", "dii_")) for name in feature_names
        ),
        "news_policy": "COLLECT_FOR_FORWARD_LEARNING_ONLY; 7-day API history is insufficient for this OOS model",
        "approved": approved,
        "reasons": reasons,
    }

    final_spec = MetaIntelligenceSpec(
        validation_id=validation_id,
        version="meta-v4-direct-return",
        horizon_minutes=horizon,
        base_specs=tuple(base_specs),
        long_model=replace(long_model, validation_id=validation_id),
        short_model=replace(short_model, validation_id=validation_id),
        meta_feature_names=feature_names,
        evidence_summary=evidence_summary,
    )
    final_spec.validate()

    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f"{validation_id}.json"
    evidence_payload = {
        "validation_id": validation_id,
        "approved": approved,
        "reason": "PASS" if approved else ", ".join(reasons),
        "spec_candidate": final_spec.to_dict(),
    }
    # Candidate spec is explicitly marked unapproved in the audit file when rejected.
    if not approved:
        evidence_payload["spec_candidate"]["approved"] = False
    evidence_path.write_text(json.dumps(evidence_payload, indent=2, sort_keys=True), encoding="utf-8")

    ModelRegistry(registry_path).record(
        model_id="meta_intelligence",
        version="meta-v4-direct-return",
        validation_id=validation_id,
        metrics=holdout_evidence.metrics,
        approved=approved,
        reason="PASS" if approved else ", ".join(reasons),
    )

    if approved:
        promote_meta_spec(approved_path, final_spec)

    return MetaValidationResult(
        approved=approved,
        validation_id=validation_id,
        selected_horizon_minutes=horizon,
        development=selected_dev.development_eval,
        holdout=holdout_evidence,
        usable_symbols=usable,
        meta_feature_names=feature_names,
        reason="PASS" if approved else ", ".join(reasons),
        approved_path=str(approved_path) if approved else None,
        evidence_path=str(evidence_path),
    )


def _build_expanding_oof_rows(
    *,
    samples: Sequence[MetaSample],
    development_dates: Sequence[date],
    horizon_minutes: int,
    validation_id: str,
    minimum_train_sessions: int = 30,
    test_block_sessions: int = 5,
) -> list[MetaTrainingRow]:
    sample_dates = set(sample.session_date for sample in samples)
    dates = [d for d in development_dates if d in sample_dates]
    rows: list[MetaTrainingRow] = []
    for start in range(minimum_train_sessions, len(dates), test_block_sessions):
        test_dates = set(dates[start : start + test_block_sessions])
        if not test_dates:
            continue
        train_dates = set(dates[:start])
        train = [sample for sample in samples if sample.session_date in train_dates]
        test = [sample for sample in samples if sample.session_date in test_dates]
        if not train or not test:
            continue
        specs = _fit_base_specs(
            samples=train,
            horizon_minutes=horizon_minutes,
            validation_id=f"{validation_id}-oof-{start}",
        )
        for sample in test:
            try:
                meta = build_meta_features(
                    symbol=sample.symbol,
                    as_of=sample.as_of,
                    raw_features=sample.raw_features,
                    base_specs=specs,
                )
            except Exception:
                continue
            rows.append(MetaTrainingRow(sample=sample, meta_features=meta))
    return rows


def _fit_base_specs(
    *,
    samples: Sequence[MetaSample],
    horizon_minutes: int,
    validation_id: str,
) -> tuple[ValidatedModelSpec, ...]:
    result: list[ValidatedModelSpec] = []
    for family_id in BASE_FAMILIES:
        family = SPECIALIST_FAMILIES[family_id]
        usable = [
            sample for sample in samples
            if all(name in sample.raw_features for name in family.required_features)
        ]
        if len(usable) < 50:
            raise RuntimeError(f"{family_id}: insufficient training rows")
        for direction in (Direction.LONG, Direction.SHORT):
            rows = [
                TrainingRow(
                    features=sample.raw_features,
                    favourable_outcome=sample.net(direction) > 0,
                    realised_net_return_bps=sample.net(direction),
                )
                for sample in usable
            ]
            payload = fit_logistic_specialist(
                family_id=family_id,
                model_id=f"{family_id}_{direction.value.lower()}",
                version="meta-base-v1",
                direction=direction,
                horizon_minutes=horizon_minutes,
                validation_id=validation_id,
                rows=rows,
                C=1.0,
            )
            gross_positive = [sample.gross(direction) for sample in usable if sample.net(direction) > 0]
            gross_negative = [abs(sample.gross(direction)) for sample in usable if sample.net(direction) <= 0]
            if not gross_positive or not gross_negative:
                raise RuntimeError(f"{family_id}/{direction.value}: one-class realised outcomes")
            spec = ValidatedModelSpec(
                model_id=str(payload["model_id"]),
                version=str(payload["version"]),
                direction=direction,
                horizon_minutes=horizon_minutes,
                feature_coefficients={
                    str(k): float(v) for k, v in payload["feature_coefficients"].items()
                },
                bias=float(payload["bias"]),
                favourable_move_bps=float(median(gross_positive)),
                adverse_move_bps=float(median(gross_negative)),
                validation_id=validation_id,
                enabled=True,
            )
            spec.validate()
            result.append(spec)
    return tuple(result)


def _fit_meta_regressors(
    *,
    rows: Sequence[MetaTrainingRow],
    feature_names: Sequence[str],
    horizon_minutes: int,
    validation_id: str,
):
    """Fit direct side-specific future gross-return models.

    This deliberately replaces the old UP/DOWN classifier + median move
    reconstruction. Costs remain outside the model so the same fitted return
    forecast can be evaluated against historical known costs and the fresh live
    spread without fabricating order-book history.
    """
    try:
        import numpy as np
        from sklearn.ensemble import RandomForestRegressor
    except ImportError as exc:
        raise RuntimeError(
            "meta research requires numpy/scikit-learn from research/requirements.txt; "
            "live OCI inference remains standard-library only"
        ) from exc

    X = np.asarray(
        [[float(row.meta_features[name]) for name in feature_names] for row in rows],
        dtype=float,
    )
    output = []
    for direction in (Direction.LONG, Direction.SHORT):
        y = np.asarray([float(row.sample.gross(direction)) for row in rows], dtype=float)
        if len(y) < 50:
            raise RuntimeError(f"meta {direction.value}: insufficient direct-return rows")
        model = RandomForestRegressor(
            n_estimators=64,
            max_depth=6,
            min_samples_leaf=max(10, len(rows) // 200),
            max_features="sqrt",
            bootstrap=True,
            n_jobs=-1,
            random_state=20260903,
        )
        model.fit(X, y)
        output.append(
            export_random_forest_regressor(
                model=model,
                model_id=f"meta_{direction.value.lower()}_direct_return",
                version="meta-v4-direct-return",
                direction=direction.value,
                horizon_minutes=horizon_minutes,
                feature_names=feature_names,
                validation_id=validation_id,
            )
        )
    return output[0], output[1]


def _evaluate_portfolio(
    *,
    rows: Sequence[MetaTrainingRow],
    long_model,
    short_model,
    feature_names: Sequence[str],
    cost_bps: float,
    horizon_minutes: int,
    starting_capital_inr: Decimal,
    max_risk_per_trade_inr: Decimal,
    hard_daily_loss_limit_inr: Decimal,
    max_aggregate_open_risk_inr: Decimal,
    max_position_fraction: float,
) -> PortfolioEvidence:
    if not rows:
        raise RuntimeError("cannot evaluate empty meta rows")
    if starting_capital_inr <= 0:
        raise ValueError("starting_capital_inr must be positive")

    scored: list[tuple[MetaTrainingRow, Direction, float, float, float]] = []
    by_time: dict[datetime, list[tuple[MetaTrainingRow, Direction, float, float, float]]] = {}
    for row in rows:
        selected = {name: float(row.meta_features[name]) for name in feature_names}
        long_gross_pred = long_model.expected_gross_return_bps(selected)
        short_gross_pred = short_model.expected_gross_return_bps(selected)
        long_net_pred = long_gross_pred - cost_bps
        short_net_pred = short_gross_pred - cost_bps
        long_p = long_model.probability_above(selected, cost_bps)
        short_p = short_model.probability_above(selected, cost_bps)
        if long_net_pred >= short_net_pred:
            direction, predicted, probability = Direction.LONG, long_net_pred, long_p
        else:
            direction, predicted, probability = Direction.SHORT, short_net_pred, short_p
        actual = row.sample.net(direction)
        item = (row, direction, predicted, probability, actual)
        scored.append(item)
        by_time.setdefault(row.sample.as_of, []).append(item)

    # Cross-sectional IC must be cross-sectional: calculate it independently at
    # each timestamp and average the genuine simultaneous ranking correlations.
    timestamp_ics = []
    for items in by_time.values():
        if len(items) < 3:
            continue
        timestamp_ics.append(
            _spearman([item[2] for item in items], [item[4] for item in items])
        )
    rank_ic = mean(timestamp_ics) if timestamp_ics else 0.0

    max_fraction = min(0.49, float(max_risk_per_trade_inr / starting_capital_inr))
    allocator = AdaptiveCapitalAllocator(
        base_risk_fraction=max_fraction,
        max_risk_fraction=max_fraction,
        max_position_fraction=max_position_fraction,
        hard_daily_loss_limit_inr=hard_daily_loss_limit_inr,
        max_aggregate_open_risk_inr=max_aggregate_open_risk_inr,
    )

    # Virtual reservations use the exact same capital allocator as live. Historical
    # bid/ask is unavailable, so quantity/spread is deliberately NOT fabricated;
    # capital/risk reservations are conservative allocation maxima.
    equity = starting_capital_inr
    peak = starting_capital_inr
    open_positions: list[tuple[datetime, Decimal, Decimal, float, date]] = []
    daily_pnl: dict[date, Decimal] = {}
    selected_outcomes: list[PredictionOutcome] = []
    selected_session_returns: dict[date, float] = {}

    for as_of in sorted(by_time):
        still_open = []
        for exit_at, reserved_capital, reserved_risk, actual_bps, session_date in open_positions:
            if exit_at <= as_of:
                pnl = reserved_capital * Decimal(str(actual_bps)) / Decimal("10000")
                equity += pnl
                peak = max(peak, equity)
                daily_pnl[session_date] = daily_pnl.get(session_date, Decimal("0")) + pnl
            else:
                still_open.append((exit_at, reserved_capital, reserved_risk, actual_bps, session_date))
        open_positions = still_open

        reserved_cash = sum((p[1] for p in open_positions), Decimal("0"))
        open_risk = sum((p[2] for p in open_positions), Decimal("0"))
        session_date = as_of.astimezone(INDIA).date()
        capital = CapitalState(
            equity_inr=equity,
            available_cash_inr=max(Decimal("0"), equity - reserved_cash),
            peak_equity_inr=peak,
            open_risk_inr=open_risk,
            daily_net_pnl_inr=daily_pnl.get(session_date, Decimal("0")),
        )

        candidates = sorted(by_time[as_of], key=lambda item: item[2], reverse=True)
        for row, direction, predicted, probability, actual in candidates:
            if predicted <= 0:
                break
            opportunity = Opportunity.create(
                symbol=row.sample.symbol,
                direction=direction,
                as_of=as_of,
                expected_net_return_bps=float(predicted),
                confidence=float(probability),
                status=DecisionStatus.QUALIFIED,
                reason="HISTORICAL_META_NET_EDGE_POSITIVE",
                opinion_ids=(),
            )
            allocation = allocator.allocate(
                opportunity=opportunity,
                capital=capital,
                estimated_volatility_bps=float(row.sample.raw_features["stock_session_range_bps"]),
            )
            if not allocation.approved:
                continue
            selected_outcomes.append(
                PredictionOutcome(
                    predicted_probability=float(probability),
                    realised_net_return_bps=float(actual),
                )
            )
            selected_session_returns[session_date] = (
                selected_session_returns.get(session_date, 0.0) + float(actual)
            )
            open_positions.append(
                (
                    as_of + timedelta(minutes=horizon_minutes),
                    allocation.capital_inr,
                    allocation.max_loss_inr,
                    float(actual),
                    session_date,
                )
            )
            capital = capital.reserve(
                capital_inr=allocation.capital_inr,
                risk_inr=allocation.max_loss_inr,
            )

    all_actual = [item[4] for item in scored]
    baseline_rate = mean(1.0 if actual > 0 else 0.0 for actual in all_actual)
    baseline_brier = mean(
        (baseline_rate - (1.0 if actual > 0 else 0.0)) ** 2
        for actual in all_actual
    )
    if selected_outcomes:
        metrics = evaluate_predictions(selected_outcomes)
    else:
        metrics = ValidationMetrics(
            observations=0,
            avg_net_return_bps=0.0,
            median_net_return_bps=0.0,
            win_rate=0.0,
            profit_factor=None,
            max_drawdown_bps=0.0,
            brier_score=baseline_brier,
            return_stability=0.0,
        )
    ci_low, ci_high = _bootstrap_session_mean_ci(tuple(selected_session_returns.values()))
    return PortfolioEvidence(
        metrics=metrics,
        selected_sessions=len(selected_session_returns),
        total_net_return_bps=sum(o.realised_net_return_bps for o in selected_outcomes),
        rank_ic=float(rank_ic),
        session_ci95_low_bps=ci_low,
        session_ci95_high_bps=ci_high,
        baseline_brier=baseline_brier,
    )


def _gross_with_range_stop(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    bars: Sequence[IntradayCandle],
    direction: Direction,
    stop_bps: float,
) -> float:
    stop_fraction = Decimal(str(stop_bps)) / Decimal("10000")
    if direction == Direction.LONG:
        stop = entry_price * (Decimal("1") - stop_fraction)
        if any(bar.low <= stop for bar in bars):
            return -float(stop_bps)
        return float((exit_price / entry_price - Decimal("1")) * Decimal("10000"))
    if direction == Direction.SHORT:
        stop = entry_price * (Decimal("1") + stop_fraction)
        if any(bar.high >= stop for bar in bars):
            return -float(stop_bps)
        return float((entry_price - exit_price) / entry_price * Decimal("10000"))
    raise ValueError("FLAT cannot be simulated")


def _spearman(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    rx = _ranks(x)
    ry = _ranks(y)
    mx = mean(rx)
    my = mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den_x = sum((a - mx) ** 2 for a in rx)
    den_y = sum((b - my) ** 2 for b in ry)
    if den_x <= 0 or den_y <= 0:
        return 0.0
    return num / (den_x * den_y) ** 0.5


def _ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        rank = (cursor + 1 + end) / 2.0
        for idx in range(cursor, end):
            ranks[ordered[idx][0]] = rank
        cursor = end
    return ranks


def _bootstrap_session_mean_ci(values: Sequence[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(20260903)
    n = len(values)
    boot = []
    for _ in range(2000):
        boot.append(mean(values[rng.randrange(n)] for _ in range(n)))
    boot.sort()
    low = boot[int(0.025 * (len(boot) - 1))]
    high = boot[int(0.975 * (len(boot) - 1))]
    return float(low), float(high)


def _portfolio_to_dict(evidence: PortfolioEvidence) -> dict:
    return {
        "metrics": asdict(evidence.metrics),
        "selected_sessions": evidence.selected_sessions,
        "total_net_return_bps": evidence.total_net_return_bps,
        "rank_ic": evidence.rank_ic,
        "session_ci95_low_bps": evidence.session_ci95_low_bps,
        "session_ci95_high_bps": evidence.session_ci95_high_bps,
        "baseline_brier": evidence.baseline_brier,
    }
