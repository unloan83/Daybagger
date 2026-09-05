from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import mean
from typing import Iterable, Mapping, Sequence

from daybagger.config import Settings
from daybagger.decision.baseline import BaselineDecision, RelativeStrengthBaselineDecider
from daybagger.decision.risk import AdaptiveCapitalAllocator, CapitalState, ExecutionSizer
from daybagger.domain import Direction, ExecutableQuote
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.operations.baseline_review import PromotionAssessment, PromotionBar, assess_promotion, classify_reject_reason
from daybagger.validation.meta_intelligence import MetaSample


@dataclass(frozen=True, slots=True)
class ReplayDay:
    session_date: date
    qualified: int
    executed: int
    avg_predicted_edge_bps: float | None
    realized_net_pnl_inr: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "session_date": self.session_date.isoformat(),
            "qualified": self.qualified,
            "executed": self.executed,
            "avg_predicted_edge_bps": self.avg_predicted_edge_bps,
            "realized_net_pnl_inr": str(self.realized_net_pnl_inr),
        }


@dataclass(frozen=True, slots=True)
class ReplayScenario:
    spread_bps: float
    sessions: int
    selected_sessions: int
    qualified: int
    executed: int
    mean_predicted_edge_bps: float | None
    mean_realized_net_bps: float | None
    total_net_pnl_inr: Decimal
    max_drawdown_fraction: float
    reject_buckets: Mapping[str, int]
    days: tuple[ReplayDay, ...]
    promotion: PromotionAssessment

    def to_dict(self) -> dict[str, object]:
        return {
            "spread_bps": self.spread_bps,
            "sessions": self.sessions,
            "selected_sessions": self.selected_sessions,
            "qualified": self.qualified,
            "executed": self.executed,
            "mean_predicted_edge_bps": self.mean_predicted_edge_bps,
            "mean_realized_net_bps": self.mean_realized_net_bps,
            "total_net_pnl_inr": str(self.total_net_pnl_inr),
            "max_drawdown_fraction": self.max_drawdown_fraction,
            "reject_buckets": dict(self.reject_buckets),
            "days": [day.to_dict() for day in self.days],
            "promotion": {
                "passed": self.promotion.passed,
                "failures": list(self.promotion.failures),
            },
        }


@dataclass(frozen=True, slots=True)
class BaselineReplayReport:
    statutory_cost_bps: float
    scenarios: tuple[ReplayScenario, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "statutory_cost_bps": self.statutory_cost_bps,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
        }


@dataclass(slots=True)
class _OpenReplayTrade:
    exit_time: datetime
    reserved_capital_inr: Decimal
    reserved_risk_inr: Decimal
    pnl_inr: Decimal


@dataclass(frozen=True, slots=True)
class _ReplayCandidate:
    sample: MetaSample
    decision: BaselineDecision
    volatility_bps: float


def replay_baseline_samples(
    *,
    samples: Sequence[MetaSample],
    settings: Settings,
    spread_scenarios_bps: Iterable[float],
    statutory_cost_bps: float,
    horizon_minutes: int,
    promotion_bar: PromotionBar | None = None,
) -> BaselineReplayReport:
    scenarios = tuple(
        _simulate_scenario(
            samples=samples,
            settings=settings,
            spread_bps=float(spread_bps),
            statutory_cost_bps=statutory_cost_bps,
            horizon_minutes=horizon_minutes,
            promotion_bar=promotion_bar or PromotionBar(),
        )
        for spread_bps in spread_scenarios_bps
    )
    return BaselineReplayReport(
        statutory_cost_bps=statutory_cost_bps,
        scenarios=scenarios,
    )


def _simulate_scenario(
    *,
    samples: Sequence[MetaSample],
    settings: Settings,
    spread_bps: float,
    statutory_cost_bps: float,
    horizon_minutes: int,
    promotion_bar: PromotionBar,
) -> ReplayScenario:
    decider = RelativeStrengthBaselineDecider(
        horizon_minutes=horizon_minutes,
        paper_slippage_bps_per_side=settings.execution.paper_slippage_bps,
    )
    cost_model = IndiaEquityIntradayCostModel()
    sizer = ExecutionSizer(cost_model)
    equity = Decimal(str(settings.capital.starting_capital_inr))
    capital = CapitalState(
        equity_inr=equity,
        available_cash_inr=equity,
        peak_equity_inr=equity,
    )
    open_trades: list[_OpenReplayTrade] = []
    reject_buckets: Counter[str] = Counter()
    realized_net_bps: list[float] = []
    predicted_edges: list[float] = []
    days: list[ReplayDay] = []
    max_drawdown_fraction = 0.0

    samples_by_day: dict[date, list[MetaSample]] = defaultdict(list)
    for sample in samples:
        samples_by_day[sample.session_date].append(sample)

    for session_date in sorted(samples_by_day):
        day_samples = sorted(samples_by_day[session_date], key=lambda sample: (sample.as_of, sample.symbol))
        capital = CapitalState(
            equity_inr=capital.equity_inr,
            available_cash_inr=capital.available_cash_inr,
            peak_equity_inr=capital.peak_equity_inr,
            open_risk_inr=capital.open_risk_inr,
            daily_net_pnl_inr=Decimal("0"),
        )
        day_qualified = 0
        day_executed = 0
        day_predicted: list[float] = []
        grouped: dict[datetime, list[MetaSample]] = defaultdict(list)
        for sample in day_samples:
            grouped[sample.as_of].append(sample)

        for as_of in sorted(grouped):
            capital = _settle_ready(open_trades, capital=capital, ready_at=as_of)
            max_drawdown_fraction = max(max_drawdown_fraction, capital.drawdown_fraction)
            candidates: list[_ReplayCandidate] = []
            for sample in grouped[as_of]:
                decision = decider.decide(
                    symbol=sample.symbol,
                    as_of=sample.as_of,
                    raw_features=sample.raw_features,
                    statutory_cost_bps=statutory_cost_bps,
                    live_spread_bps=spread_bps,
                )
                if decision.opportunity.status.value != "QUALIFIED":
                    reject_buckets[classify_reject_reason(decision.opportunity.reason)] += 1
                    continue
                candidates.append(
                    _ReplayCandidate(
                        sample=sample,
                        decision=decision,
                        volatility_bps=float(sample.raw_features["stock_session_range_bps"]),
                    )
                )
            candidates.sort(
                key=lambda candidate: (
                    candidate.decision.opportunity.expected_net_return_bps,
                    candidate.decision.opportunity.confidence,
                ),
                reverse=True,
            )
            day_qualified += len(candidates)
            allocator = _allocator(settings=settings, capital=capital)
            for candidate in candidates:
                allocation = allocator.allocate(
                    opportunity=candidate.decision.opportunity,
                    capital=capital,
                    estimated_volatility_bps=candidate.volatility_bps,
                )
                if not allocation.approved:
                    reject_buckets[classify_reject_reason(allocation.reason)] += 1
                    continue
                quote = ExecutableQuote(
                    symbol=candidate.sample.symbol,
                    as_of=candidate.sample.as_of,
                    bid=candidate.sample.entry_price,
                    ask=candidate.sample.entry_price,
                    last=candidate.sample.entry_price,
                )
                sized = sizer.size(
                    opportunity=candidate.decision.opportunity,
                    allocation=allocation,
                    quote=quote,
                    estimated_volatility_bps=candidate.volatility_bps,
                    slippage_bps=settings.execution.paper_slippage_bps,
                )
                if not sized.approved:
                    reject_buckets[classify_reject_reason(sized.reason)] += 1
                    continue
                realized_bps = (
                    candidate.sample.gross(candidate.decision.opportunity.direction)
                    - statutory_cost_bps
                    - spread_bps
                    - 2.0 * settings.execution.paper_slippage_bps
                )
                pnl_inr = sized.entry_notional_inr * Decimal(str(realized_bps)) / Decimal("10000")
                capital = capital.reserve(
                    capital_inr=sized.entry_notional_inr,
                    risk_inr=sized.estimated_adverse_loss_inr + sized.estimated_round_trip_cost_inr,
                )
                open_trades.append(
                    _OpenReplayTrade(
                        exit_time=candidate.sample.as_of + timedelta(minutes=horizon_minutes),
                        reserved_capital_inr=sized.entry_notional_inr,
                        reserved_risk_inr=sized.estimated_adverse_loss_inr + sized.estimated_round_trip_cost_inr,
                        pnl_inr=pnl_inr,
                    )
                )
                day_executed += 1
                predicted_edges.append(candidate.decision.opportunity.expected_net_return_bps)
                day_predicted.append(candidate.decision.opportunity.expected_net_return_bps)
                realized_net_bps.append(realized_bps)
            capital = _settle_ready(open_trades, capital=capital, ready_at=as_of)
            max_drawdown_fraction = max(max_drawdown_fraction, capital.drawdown_fraction)

        if day_samples:
            capital = _settle_ready(
                open_trades,
                capital=capital,
                ready_at=day_samples[-1].as_of + timedelta(minutes=horizon_minutes),
            )
            max_drawdown_fraction = max(max_drawdown_fraction, capital.drawdown_fraction)
        days.append(
            ReplayDay(
                session_date=session_date,
                qualified=day_qualified,
                executed=day_executed,
                avg_predicted_edge_bps=(mean(day_predicted) if day_predicted else None),
                realized_net_pnl_inr=capital.daily_net_pnl_inr,
            )
        )

    daily_reviews = tuple(
        _to_review_day(day) for day in days
    )
    promotion = assess_promotion(daily_reviews, promotion_bar)
    active_days = [day for day in days if day.executed > 0]
    total_net = sum((day.realized_net_pnl_inr for day in days), start=Decimal("0"))
    return ReplayScenario(
        spread_bps=spread_bps,
        sessions=len(days),
        selected_sessions=len(active_days),
        qualified=sum(day.qualified for day in days),
        executed=sum(day.executed for day in days),
        mean_predicted_edge_bps=(mean(predicted_edges) if predicted_edges else None),
        mean_realized_net_bps=(mean(realized_net_bps) if realized_net_bps else None),
        total_net_pnl_inr=total_net,
        max_drawdown_fraction=max_drawdown_fraction,
        reject_buckets={bucket: reject_buckets.get(bucket, 0) for bucket in ("regime", "spread_cost", "allocation", "execution", "evidence", "other")},
        days=tuple(days),
        promotion=promotion,
    )


def _allocator(*, settings: Settings, capital: CapitalState) -> AdaptiveCapitalAllocator:
    absolute_max = Decimal(str(settings.risk.max_risk_per_trade_inr))
    max_fraction = min(0.49, float(absolute_max / capital.equity_inr)) if capital.equity_inr > 0 else 0.0
    if max_fraction <= 0:
        max_fraction = 0.0001
    return AdaptiveCapitalAllocator(
        base_risk_fraction=max_fraction,
        max_risk_fraction=max_fraction,
        max_position_fraction=settings.risk.max_position_fraction,
        hard_daily_loss_limit_inr=Decimal(str(settings.risk.hard_daily_loss_limit_inr)),
        max_aggregate_open_risk_inr=Decimal(str(settings.risk.max_aggregate_open_risk_inr)),
    )


def _settle_ready(open_trades: list[_OpenReplayTrade], *, capital: CapitalState, ready_at: datetime) -> CapitalState:
    remaining: list[_OpenReplayTrade] = []
    updated = capital
    for trade in open_trades:
        if trade.exit_time > ready_at:
            remaining.append(trade)
            continue
        equity = updated.equity_inr + trade.pnl_inr
        peak = max(updated.peak_equity_inr, equity)
        updated = CapitalState(
            equity_inr=equity,
            available_cash_inr=updated.available_cash_inr + trade.reserved_capital_inr + trade.pnl_inr,
            peak_equity_inr=peak,
            open_risk_inr=max(Decimal("0"), updated.open_risk_inr - trade.reserved_risk_inr),
            daily_net_pnl_inr=updated.daily_net_pnl_inr + trade.pnl_inr,
        )
    open_trades[:] = remaining
    return updated


def _to_review_day(day: ReplayDay):
    from daybagger.operations.baseline_review import DailyBaselineReview

    return DailyBaselineReview(
        session_date=day.session_date,
        scanned=0,
        executable=0,
        aligned=0,
        qualified=day.qualified,
        rejected_regime=0,
        rejected_spread_cost=0,
        rejected_allocation=0,
        rejected_execution=0,
        rejected_evidence=0,
        rejected_other=0,
        executed=day.executed,
        closed_trades=day.executed,
        avg_predicted_edge_bps=day.avg_predicted_edge_bps,
        realized_gross_pnl_inr=day.realized_net_pnl_inr,
        realized_net_pnl_inr=day.realized_net_pnl_inr,
    )
