from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from daybagger.config import Settings
from daybagger.data.universe import NSEEquityUniverse, ObservableEquity, usable_for_execution
from daybagger.data.upstox import IntradayCandle, UpstoxDataError, UpstoxMarketData
from daybagger.decision.baseline import (
    BASELINE_MODEL_ID,
    BaselineDecision,
    RelativeStrengthBaselineDecider,
)
from daybagger.decision.risk import (
    AdaptiveCapitalAllocator,
    AllocationDecision,
    CapitalState,
    ExecutionSizer,
)
from daybagger.domain import DecisionStatus, Direction, ExecutionRequest
from daybagger.execution.paper import PaperBroker
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.intelligence.meta_features import (
    CrossSectionState,
    build_cross_section_state,
    build_meta_raw_features,
)
from daybagger.intelligence.upstox_external import (
    UpstoxExternalIntelligence,
    load_sector_cache,
    save_sector_cache,
)
from daybagger.operations.outcomes import OutcomeLearner
from daybagger.operations.trace_store import DecisionTraceStore
from daybagger.decision.learning import ModelLearningStore
from daybagger.runtime.ledger import PaperLedger, Position
from daybagger.runtime.session import SessionGuard, SessionState
from daybagger.validation.historical import HistoricalCandleClient


INDIA = ZoneInfo("Asia/Kolkata")
NIFTY_KEY = "NSE_INDEX|Nifty 50"
BANK_NIFTY_KEY = "NSE_INDEX|Nifty Bank"
INDIA_VIX_KEY = "NSE_INDEX|India VIX"
BASELINE_VALIDATION_ID = "baseline-relative-strength-paper-v1"


class PaperRuntimeError(RuntimeError):
    """Paper runtime cannot proceed without violating a Daybagger invariant."""


@dataclass(frozen=True, slots=True)
class RuntimeCandidate:
    observed: ObservableEquity
    decision: BaselineDecision
    raw_features: Mapping[str, float]
    volatility_bps: float


@dataclass(frozen=True, slots=True)
class RuntimeUniverseStage:
    observed: tuple[ObservableEquity, ...]
    deep: tuple[ObservableEquity, ...]
    sectors: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RuntimeFeatureStage:
    as_of: datetime
    market_prefix: tuple[IntradayCandle, ...]
    bank_prefix: tuple[IntradayCandle, ...]
    vix_prefix: tuple[IntradayCandle, ...]
    stock_prefixes: Mapping[str, tuple[IntradayCandle, ...]]
    cross_section: CrossSectionState
    observed_by_symbol: Mapping[str, ObservableEquity]
    stock_candles: Mapping[str, tuple[IntradayCandle, ...]]
    sectors: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RuntimeExecutionStage:
    fills: int
    no_trade_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeCycleResult:
    as_of: datetime
    observed_universe: int
    deep_symbols: int
    decisions: int
    qualified: int
    fills: int
    exits: int
    no_trade_reasons: tuple[str, ...]


class DaybaggerPaperRuntime:
    """
    One canonical staged paper runtime.

    Broad official MIS quote scan -> resource-bounded deep candle scan ->
    deterministic cross-sectional relative-strength baseline -> sequential
    portfolio allocation -> exact quantity/cost recheck -> paper execution ->
    ledger -> outcome learning.

    Meta validation remains a separate research track until it produces proven
    out-of-sample edge.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        settings: Settings,
        market_data: UpstoxMarketData,
    ) -> None:
        if settings.app.trading_mode != "paper":
            raise PaperRuntimeError("DaybaggerPaperRuntime is paper-only")
        self.repo_root = repo_root.resolve()
        self.settings = settings
        self.market_data = market_data
        self.guard = SessionGuard(timezone=settings.app.timezone)
        self.universe = NSEEquityUniverse()
        self.external = UpstoxExternalIntelligence(market_data)
        self.historical = HistoricalCandleClient(market_data)
        self.cost_model = IndiaEquityIntradayCostModel()
        self.sizer = ExecutionSizer(self.cost_model)
        self.decider = RelativeStrengthBaselineDecider(
            horizon_minutes=15,
            paper_slippage_bps_per_side=settings.execution.paper_slippage_bps,
        )
        self.broker = PaperBroker(
            max_quote_age_seconds=settings.execution.max_quote_age_seconds,
            slippage_bps=settings.execution.paper_slippage_bps,
        )
        self.ledger = PaperLedger(self._path(settings.storage.paper_ledger_path))
        self.trace_store = DecisionTraceStore(self._path(settings.storage.decision_trace_path))
        self.learning_store = ModelLearningStore(
            self._path(settings.storage.learning_db_path),
            minimum_observations=settings.runtime.learning_min_observations,
            lookback_days=settings.runtime.learning_lookback_days,
        )
        self.ledger.initialize()
        self.trace_store.initialize()
        self.learning_store.initialize()
        self.outcome_learner = OutcomeLearner(self.learning_store)
        self.sector_cache_path = self.repo_root / "data" / "sector_cache.json"
        self._prior_sessions: dict[str, list[list[IntradayCandle]]] = {}

    def run_cycle(self, *, now: datetime | None = None) -> RuntimeCycleResult:
        current = (now or datetime.now(INDIA)).astimezone(INDIA)
        self._require_official_session(current)

        exits = self._manage_open_positions(current)
        if self.guard.state(current) != SessionState.MARKET:
            return RuntimeCycleResult(current, 0, 0, 0, 0, 0, exits, ("ENTRY_WINDOW_CLOSED",))

        mandatory_exit = current.replace(
            hour=self.guard.mandatory_exit.hour,
            minute=self.guard.mandatory_exit.minute,
            second=0,
            microsecond=0,
        )
        if current + timedelta(minutes=self.decider.horizon_minutes) > mandatory_exit:
            return RuntimeCycleResult(
                current,
                0,
                0,
                0,
                0,
                0,
                exits,
                ("BASELINE_HORIZON_EXCEEDS_MANDATORY_EXIT",),
            )

        try:
            universe = self._scan_universe()
        except PaperRuntimeError as exc:
            return RuntimeCycleResult(current, 0, 0, 0, 0, 0, exits, (str(exc),))
        try:
            features = self._build_feature_stage(universe)
        except PaperRuntimeError as exc:
            return RuntimeCycleResult(
                current,
                len(universe.observed),
                len(universe.deep),
                0,
                0,
                0,
                exits,
                (str(exc),),
            )
        candidates, pre_execution_reasons = self._build_candidates(features)
        execution = self._execute_candidates(candidates, current)
        self._learn_matured_traces(current, features.stock_candles)
        return RuntimeCycleResult(
            as_of=features.as_of,
            observed_universe=len(universe.observed),
            deep_symbols=len(features.stock_prefixes),
            decisions=len(features.stock_prefixes),
            qualified=len(candidates),
            fills=execution.fills,
            exits=exits,
            no_trade_reasons=tuple(pre_execution_reasons + list(execution.no_trade_reasons)),
        )

    def _scan_universe(self) -> RuntimeUniverseStage:
        instruments = self.universe.load_mis_equities()
        observed = self.universe.observe(
            market_data=self.market_data,
            instruments=instruments,
            batch_size=500,
            require_complete=False,
        )
        executable = [item for item in observed if usable_for_execution(item)]
        executable.sort(key=lambda item: item.session_turnover_inr, reverse=True)
        open_symbols = {pos.symbol for pos in self.ledger.open_positions()}
        deep = [
            item for item in executable
            if item.instrument.trading_symbol not in open_symbols
        ][: self.settings.runtime.deep_scan_symbols]
        if len(deep) < 6:
            raise PaperRuntimeError("INSUFFICIENT_EXECUTABLE_DEEP_UNIVERSE")
        sectors = self._resolve_sectors(deep)
        deep = [item for item in deep if item.instrument.trading_symbol in sectors]
        if len(deep) < 6:
            raise PaperRuntimeError("INSUFFICIENT_SECTOR_MAPPED_UNIVERSE")
        return RuntimeUniverseStage(
            observed=tuple(observed),
            deep=tuple(deep),
            sectors=sectors,
        )

    def _build_feature_stage(self, universe: RuntimeUniverseStage) -> RuntimeFeatureStage:
        market_candles = self.market_data.intraday_candles(NIFTY_KEY)
        bank_candles = self.market_data.intraday_candles(BANK_NIFTY_KEY)
        vix_candles = self.market_data.intraday_candles(INDIA_VIX_KEY)
        stock_candles: dict[str, tuple[IntradayCandle, ...]] = {}
        by_symbol = {item.instrument.trading_symbol: item for item in universe.deep}
        for item in universe.deep:
            try:
                stock_candles[item.instrument.trading_symbol] = tuple(
                    self.market_data.intraday_candles(item.instrument.instrument_key)
                )
            except UpstoxDataError:
                continue

        as_of = _latest_common_context_timestamp(market_candles, bank_candles, vix_candles)
        market_prefix = tuple(_prefix_at(market_candles, as_of))
        bank_prefix = tuple(_prefix_at(bank_candles, as_of))
        vix_prefix = tuple(_prefix_at(vix_candles, as_of))
        prefixes = {
            symbol: tuple(prefix)
            for symbol, candles in stock_candles.items()
            if (prefix := _prefix_at(candles, as_of)) and len(prefix) >= 30
        }
        if len(prefixes) < 6:
            raise PaperRuntimeError("INSUFFICIENT_ALIGNED_MINUTE_DATA")

        cross = build_cross_section_state(
            session_date=as_of.astimezone(INDIA).date(),
            as_of=as_of,
            prefixes_by_symbol=prefixes,
            sector_by_symbol=universe.sectors,
        )
        return RuntimeFeatureStage(
            as_of=as_of,
            market_prefix=market_prefix,
            bank_prefix=bank_prefix,
            vix_prefix=vix_prefix,
            stock_prefixes=prefixes,
            cross_section=cross,
            observed_by_symbol=by_symbol,
            stock_candles=stock_candles,
            sectors=universe.sectors,
        )

    def _build_candidates(
        self,
        features: RuntimeFeatureStage,
    ) -> tuple[list[RuntimeCandidate], list[str]]:
        candidates: list[RuntimeCandidate] = []
        no_trade: list[str] = []
        conservative_statutory_bps = self.cost_model.conservative_linear_round_trip_bps()

        for symbol, prefix in features.stock_prefixes.items():
            item = features.observed_by_symbol[symbol]
            try:
                prior = self._prior_volume_sessions(item, features.as_of.date())
                raw = build_meta_raw_features(
                    symbol=symbol,
                    stock_prefix=prefix,
                    market_prefix=features.market_prefix,
                    bank_nifty_prefix=features.bank_prefix,
                    india_vix_prefix=features.vix_prefix,
                    cross_section=features.cross_section,
                    sector=features.sectors[symbol],
                    prior_stock_sessions=prior,
                    external_numeric=None,
                )
                spread = item.spread_bps
                if spread is None or spread < 0:
                    raise PaperRuntimeError(f"{symbol}: live spread unavailable")
                decision = self.decider.decide(
                    symbol=symbol,
                    as_of=features.as_of,
                    raw_features=raw,
                    statutory_cost_bps=conservative_statutory_bps,
                    live_spread_bps=spread,
                )
            except Exception as exc:
                no_trade.append(f"{symbol}:INSUFFICIENT_EVIDENCE:{type(exc).__name__}")
                continue

            if decision.opportunity.status != DecisionStatus.QUALIFIED:
                self.trace_store.record_decision(
                    symbol=symbol,
                    instrument_key=item.instrument.instrument_key,
                    as_of=features.as_of,
                    opportunity=decision.opportunity,
                    allocation_approved=False,
                    estimated_cost_bps=decision.estimated_total_cost_bps,
                    opinions=decision.opinions,
                    validation_ids={BASELINE_MODEL_ID: BASELINE_VALIDATION_ID},
                    reference_price=item.quote.last_price,
                    features=decision.features_used,
                )
                no_trade.append(f"{symbol}:{decision.opportunity.reason}")
                continue

            candidates.append(
                RuntimeCandidate(
                    observed=item,
                    decision=decision,
                    raw_features=raw,
                    volatility_bps=float(raw["stock_session_range_bps"]),
                )
            )

        candidates.sort(
            key=lambda item: (
                item.decision.opportunity.expected_net_return_bps,
                item.decision.opportunity.confidence,
                item.decision.residual_strength_bps,
            ),
            reverse=True,
        )
        return candidates, no_trade

    def _execute_candidates(
        self,
        candidates: Sequence[RuntimeCandidate],
        current: datetime,
    ) -> RuntimeExecutionStage:
        capital = self._capital_state(current.date())
        allocator = self._allocator(capital)
        no_trade: list[str] = []
        fills = 0
        validation_ids = {BASELINE_MODEL_ID: BASELINE_VALIDATION_ID}

        for candidate in candidates:
            op = candidate.decision.opportunity
            allocation = allocator.allocate(
                opportunity=op,
                capital=capital,
                estimated_volatility_bps=candidate.volatility_bps,
            )
            if not allocation.approved:
                self._trace_candidate(candidate, allocation, validation_ids)
                no_trade.append(f"{op.symbol}:{allocation.reason}")
                continue

            instrument_key = candidate.observed.instrument.instrument_key
            try:
                fresh_snapshot = self.market_data.full_quotes([instrument_key])[instrument_key]
                fresh_quote = fresh_snapshot.to_executable_quote()
                fresh_spread = _spread_bps(fresh_quote)
                confirmed = self.decider.decide(
                    symbol=op.symbol,
                    as_of=candidate.decision.opportunity.as_of,
                    raw_features=candidate.raw_features,
                    statutory_cost_bps=self.cost_model.conservative_linear_round_trip_bps(),
                    live_spread_bps=fresh_spread,
                )
                if confirmed.opportunity.status != DecisionStatus.QUALIFIED:
                    self.trace_store.record_decision(
                        symbol=op.symbol,
                        instrument_key=instrument_key,
                        as_of=candidate.decision.opportunity.as_of,
                        opportunity=confirmed.opportunity,
                        allocation_approved=False,
                        estimated_cost_bps=confirmed.estimated_total_cost_bps,
                        opinions=confirmed.opinions,
                        validation_ids=validation_ids,
                        reference_price=fresh_snapshot.last_price,
                        features=confirmed.features_used,
                    )
                    no_trade.append(f"{op.symbol}:FRESH_BASELINE_RECHECK_REJECTED")
                    continue

                sized = self.sizer.size(
                    opportunity=confirmed.opportunity,
                    allocation=allocation,
                    quote=fresh_quote,
                    estimated_volatility_bps=candidate.volatility_bps,
                    slippage_bps=self.settings.execution.paper_slippage_bps,
                )
                if not sized.approved:
                    self._trace_candidate(
                        RuntimeCandidate(
                            observed=candidate.observed,
                            decision=confirmed,
                            raw_features=candidate.raw_features,
                            volatility_bps=candidate.volatility_bps,
                        ),
                        AllocationDecision(False, sized.reason, Decimal("0"), Decimal("0"), 0.0),
                        validation_ids,
                    )
                    no_trade.append(f"{op.symbol}:{sized.reason}")
                    continue

                execution_now = datetime.now(INDIA)
                request = ExecutionRequest.create(
                    opportunity_id=confirmed.opportunity.opportunity_id,
                    symbol=op.symbol,
                    direction=confirmed.opportunity.direction,
                    quantity=sized.quantity,
                    created_at=execution_now,
                )
                execution = self.broker.execute(
                    request=request,
                    quote=fresh_quote,
                    now=execution_now,
                )
                if execution.filled_price is None:
                    raise PaperRuntimeError(f"{op.symbol}: paper broker returned no fill")
                actual_notional = execution.filled_price * Decimal(sized.quantity)
                total_risk = sized.estimated_adverse_loss_inr + sized.estimated_round_trip_cost_inr
                if actual_notional > capital.available_cash_inr:
                    raise PaperRuntimeError(f"{op.symbol}: actual paper fill exceeds available cash")
                stop = _stop_price(
                    execution.filled_price,
                    confirmed.opportunity.direction,
                    candidate.volatility_bps,
                )
                self.ledger.open_fill(
                    symbol=op.symbol,
                    direction=confirmed.opportunity.direction,
                    quantity=sized.quantity,
                    filled_price=execution.filled_price,
                    now=execution.executed_at,
                    instrument_key=instrument_key,
                    opportunity_id=str(confirmed.opportunity.opportunity_id),
                    validation_id=BASELINE_VALIDATION_ID,
                    reserved_capital_inr=actual_notional,
                    max_loss_inr=total_risk,
                    stop_price=stop,
                    horizon_minutes=self.decider.horizon_minutes,
                )
                capital = capital.reserve(capital_inr=actual_notional, risk_inr=total_risk)
                fills += 1
                self.trace_store.record_decision(
                    symbol=op.symbol,
                    instrument_key=instrument_key,
                    as_of=candidate.decision.opportunity.as_of,
                    opportunity=confirmed.opportunity,
                    allocation_approved=True,
                    estimated_cost_bps=confirmed.estimated_total_cost_bps,
                    opinions=confirmed.opinions,
                    validation_ids=validation_ids,
                    reference_price=fresh_snapshot.last_price,
                    features=confirmed.features_used,
                )
            except Exception as exc:
                no_trade.append(f"{op.symbol}:EXECUTION_FAIL_CLOSED:{type(exc).__name__}")
                continue

        return RuntimeExecutionStage(fills=fills, no_trade_reasons=tuple(no_trade))

    def _path(self, configured: str) -> Path:
        path = Path(configured)
        return path if path.is_absolute() else self.repo_root / path

    def _require_official_session(self, now: datetime) -> None:
        if self.guard.state(now) == SessionState.NON_TRADING_DAY:
            raise PaperRuntimeError("NON_TRADING_DAY")
        timings = self.market_data.market_timings(now.date())
        nse = [item for item in timings if item.exchange.upper().startswith("NSE")]
        if not nse:
            raise PaperRuntimeError("OFFICIAL_NSE_MARKET_TIMING_ABSENT")
        status = self.market_data.exchange_status("NSE").upper()
        if "CLOSE" in status or "HALT" in status:
            raise PaperRuntimeError(f"NSE_EXCHANGE_STATUS_{status}")

    def _resolve_sectors(self, deep: Sequence[ObservableEquity]) -> dict[str, str]:
        cache = load_sector_cache(self.sector_cache_path)
        changed = False
        result: dict[str, str] = {}
        for item in deep:
            isin = item.instrument.isin
            sector = cache.get(isin)
            if not sector:
                try:
                    sector = self.external.company_sector(isin)
                except Exception:
                    continue
                cache[isin] = sector
                changed = True
            result[item.instrument.trading_symbol] = sector
        if changed:
            save_sector_cache(self.sector_cache_path, cache)
        return result

    def _prior_volume_sessions(
        self,
        item: ObservableEquity,
        session_date: date,
    ) -> list[list[IntradayCandle]]:
        key = item.instrument.instrument_key
        if key in self._prior_sessions:
            return self._prior_sessions[key]
        candles = self.historical.fetch(
            key,
            from_date=session_date - timedelta(days=35),
            to_date=session_date - timedelta(days=1),
        )
        grouped: dict[date, list[IntradayCandle]] = {}
        for candle in candles:
            grouped.setdefault(candle.timestamp.astimezone(INDIA).date(), []).append(candle)
        sessions = [sorted(grouped[d], key=lambda c: c.timestamp) for d in sorted(grouped)][-20:]
        if len(sessions) < 5:
            raise PaperRuntimeError(f"{item.instrument.trading_symbol}: insufficient prior volume sessions")
        self._prior_sessions[key] = sessions
        return sessions

    def _capital_state(self, session_date: date) -> CapitalState:
        starting = Decimal(str(self.settings.capital.starting_capital_inr))
        equity, peak = self.ledger.equity_and_peak(starting)
        _, daily_net = self.ledger.realised_pnl_for_date(
            session_date,
            timezone_name=self.settings.app.timezone,
        )
        if daily_net is None:
            raise PaperRuntimeError("DAILY_NET_PNL_UNAVAILABLE")
        available = max(Decimal("0"), equity - self.ledger.open_capital_inr())
        return CapitalState(
            equity_inr=equity,
            available_cash_inr=available,
            peak_equity_inr=peak,
            open_risk_inr=self.ledger.open_risk_inr(),
            daily_net_pnl_inr=daily_net,
        )

    def _allocator(self, capital: CapitalState) -> AdaptiveCapitalAllocator:
        if capital.equity_inr <= 0:
            raise PaperRuntimeError("NO_POSITIVE_EQUITY")
        absolute_max = Decimal(str(self.settings.risk.max_risk_per_trade_inr))
        max_fraction = min(0.49, float(absolute_max / capital.equity_inr))
        if max_fraction <= 0:
            raise PaperRuntimeError("NO_RISK_BUDGET")
        return AdaptiveCapitalAllocator(
            base_risk_fraction=max_fraction,
            max_risk_fraction=max_fraction,
            max_position_fraction=self.settings.risk.max_position_fraction,
            hard_daily_loss_limit_inr=Decimal(str(self.settings.risk.hard_daily_loss_limit_inr)),
            max_aggregate_open_risk_inr=Decimal(str(self.settings.risk.max_aggregate_open_risk_inr)),
        )

    def _trace_candidate(
        self,
        candidate: RuntimeCandidate,
        allocation: AllocationDecision,
        validation_ids: Mapping[str, str],
    ) -> None:
        item = candidate.observed
        self.trace_store.record_decision(
            symbol=item.instrument.trading_symbol,
            instrument_key=item.instrument.instrument_key,
            as_of=candidate.decision.opportunity.as_of,
            opportunity=candidate.decision.opportunity,
            allocation_approved=allocation.approved,
            estimated_cost_bps=candidate.decision.estimated_total_cost_bps,
            opinions=candidate.decision.opinions,
            validation_ids=validation_ids,
            reference_price=item.quote.last_price,
            features=candidate.decision.features_used,
        )

    def _manage_open_positions(self, now: datetime) -> int:
        positions = self.ledger.open_positions()
        if not positions:
            return 0
        keys = [pos.instrument_key for pos in positions if pos.instrument_key]
        if len(keys) != len(positions):
            raise PaperRuntimeError("OPEN_POSITION_MISSING_INSTRUMENT_KEY")
        snapshots = self.market_data.full_quotes(keys, require_complete=True)
        exits = 0
        mandatory_exit = now.replace(
            hour=self.guard.mandatory_exit.hour,
            minute=self.guard.mandatory_exit.minute,
            second=0,
            microsecond=0,
        )
        for pos in positions:
            snap = snapshots[pos.instrument_key]
            quote = snap.to_executable_quote()
            reason = _exit_reason(pos, quote, now, mandatory_exit)
            if reason is None:
                continue
            fill = self.broker.exit_fill_price(
                position_direction=pos.direction,
                quote=quote,
                now=now,
            )
            entry_turnover = pos.entry_price * Decimal(pos.quantity)
            exit_turnover = fill * Decimal(pos.quantity)
            if pos.direction == Direction.LONG:
                buy_turnover, sell_turnover = entry_turnover, exit_turnover
            else:
                buy_turnover, sell_turnover = exit_turnover, entry_turnover
            costs = self.cost_model.estimate_round_trip(
                buy_turnover=buy_turnover,
                sell_turnover=sell_turnover,
            )
            self.ledger.close_fill(
                position_id=pos.position_id,
                filled_price=fill,
                now=now,
                costs_inr=costs.total,
                exit_reason=reason,
            )
            exits += 1
        return exits

    def _learn_matured_traces(
        self,
        now: datetime,
        current_candles: Mapping[str, Sequence[IntradayCandle]],
    ) -> None:
        pending = self.trace_store.pending_outcomes(ready_at=now)
        fetched: dict[str, Sequence[IntradayCandle]] = dict(current_candles)
        extra_fetches = 0
        for item in pending:
            candles = fetched.get(item.symbol)
            if candles is None and item.instrument_key and extra_fetches < 20:
                try:
                    candles = self.market_data.intraday_candles(item.instrument_key)
                    fetched[item.symbol] = candles
                    extra_fetches += 1
                except Exception:
                    continue
            if not candles:
                continue
            outcomes = self.outcome_learner.record_aligned_outcomes(
                opinions=item.opinions,
                decision_at=item.as_of,
                session_candles=candles,
                estimated_cost_bps=item.estimated_cost_bps,
            )
            if len(outcomes) == len(item.opinions):
                self.trace_store.mark_outcome_recorded(item.trace_id)



def _latest_common_context_timestamp(*series: Sequence[IntradayCandle]) -> datetime:
    if not series or any(not values for values in series):
        raise PaperRuntimeError("CONTEXT_CANDLES_MISSING")
    common = set(c.timestamp for c in series[0])
    for values in series[1:]:
        common.intersection_update(c.timestamp for c in values)
    if not common:
        raise PaperRuntimeError("CONTEXT_TIMESTAMPS_NOT_ALIGNED")
    return max(common)



def _prefix_at(candles: Sequence[IntradayCandle], as_of: datetime) -> list[IntradayCandle]:
    ordered = sorted((c for c in candles if c.timestamp <= as_of), key=lambda c: c.timestamp)
    if not ordered or ordered[-1].timestamp != as_of:
        return []
    return ordered



def _spread_bps(quote) -> float:
    mid = (quote.bid + quote.ask) / Decimal("2")
    if mid <= 0:
        raise PaperRuntimeError("INVALID_LIVE_MID")
    return float((quote.ask - quote.bid) / mid * Decimal("10000"))



def _stop_price(entry: Decimal, direction: Direction, volatility_bps: float) -> Decimal:
    distance = entry * Decimal(str(volatility_bps)) / Decimal("10000")
    if distance <= 0:
        raise PaperRuntimeError("NON_POSITIVE_STOP_DISTANCE")
    return entry - distance if direction == Direction.LONG else entry + distance



def _exit_reason(
    pos: Position,
    quote,
    now: datetime,
    mandatory_exit: datetime,
) -> str | None:
    if now >= mandatory_exit:
        return "MANDATORY_1510_EXIT"
    if pos.horizon_minutes is not None and now >= pos.opened_at + timedelta(minutes=pos.horizon_minutes):
        return "MODEL_HORIZON_EXIT"
    if pos.stop_price is not None:
        if pos.direction == Direction.LONG and quote.bid <= pos.stop_price:
            return "RANGE_STOP"
        if pos.direction == Direction.SHORT and quote.ask >= pos.stop_price:
            return "RANGE_STOP"
    return None
