from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from daybagger.data.upstox import IntradayCandle
from daybagger.domain import Direction
from daybagger.intelligence.meta_features import (
    MetaFeatureError,
    build_cross_section_state,
    build_meta_raw_features,
)
from daybagger.intelligence.upstox_external import (
    _parse_dii_payload,
    _parse_fii_payload,
    lagged_institutional_features,
)
from daybagger.meta.forest import export_random_forest_regressor
from daybagger.validation.meta_intelligence import _gross_with_range_stop, _spearman, _triple_barrier_gross


INDIA = ZoneInfo("Asia/Kolkata")


def _bars(key: str, start: datetime, *, n: int, base: str, step: str = "0.10"):
    price = Decimal(base)
    inc = Decimal(step)
    rows = []
    for i in range(n):
        o = price
        c = price + inc
        rows.append(
            IntradayCandle(
                instrument_key=key,
                timestamp=start + timedelta(minutes=i),
                open=o,
                high=max(o, c) + Decimal("0.05"),
                low=min(o, c) - Decimal("0.05"),
                close=c,
                volume=1000 + i * 10,
                open_interest=0,
            )
        )
        price = c
    return rows


def test_exported_forest_matches_sklearn_regression_and_tree_vote_probability():
    np = pytest.importorskip("numpy")
    ensemble = pytest.importorskip("sklearn.ensemble")
    X = np.asarray([[0.0, 0.0], [0.0, 1.0], [1.0, 0.0], [1.0, 1.0]] * 20)
    y = np.asarray([-10.0, 5.0, 20.0, 35.0] * 20)
    model = ensemble.RandomForestRegressor(
        n_estimators=8,
        max_depth=3,
        random_state=7,
    ).fit(X, y)
    spec = export_random_forest_regressor(
        model=model,
        model_id="meta_long_direct_return",
        version="meta-v4-direct-return",
        direction="LONG",
        horizon_minutes=30,
        feature_names=("a", "b"),
        validation_id="test",
    )
    for row in ([0.0, 0.0], [0.2, 0.8], [1.0, 1.0]):
        expected = float(model.predict([row])[0])
        actual = spec.expected_gross_return_bps({"a": row[0], "b": row[1]})
        assert actual == pytest.approx(expected, abs=1e-12)
        tree_preds = [float(est.predict([row])[0]) for est in model.estimators_]
        expected_probability = sum(v > 8.0 for v in tree_preds) / len(tree_preds)
        assert spec.probability_above({"a": row[0], "b": row[1]}, 8.0) == pytest.approx(expected_probability)
        summary = spec.prediction_summary({"a": row[0], "b": row[1]}, 8.0)
        assert summary[0] == pytest.approx(expected, abs=1e-12)
        assert summary[1] == pytest.approx(expected_probability)


def test_meta_features_are_timestamp_aligned_and_volume_uses_prior_sessions_only():
    as_of_start = datetime(2026, 9, 3, 9, 15, tzinfo=INDIA)
    stock = _bars("S", as_of_start, n=35, base="100")
    market = _bars("M", as_of_start, n=35, base="25000", step="1")
    bank = _bars("B", as_of_start, n=35, base="55000", step="2")
    vix = _bars("V", as_of_start, n=35, base="12", step="0.01")
    others = {
        "S": stock,
        "A": _bars("A", as_of_start, n=35, base="200", step="0.05"),
        "BETA": _bars("C", as_of_start, n=35, base="300", step="-0.02"),
    }
    sectors = {"S": "Tech", "A": "Tech", "BETA": "Banks"}
    cross = build_cross_section_state(
        session_date=date(2026, 9, 3),
        as_of=stock[-1].timestamp,
        prefixes_by_symbol=others,
        sector_by_symbol=sectors,
    )
    prior = []
    for days_ago in range(1, 7):
        start = as_of_start - timedelta(days=days_ago)
        prior.append(_bars("S", start, n=35, base="99"))
    features = build_meta_raw_features(
        symbol="S",
        stock_prefix=stock,
        market_prefix=market,
        bank_nifty_prefix=bank,
        india_vix_prefix=vix,
        cross_section=cross,
        sector="Tech",
        prior_stock_sessions=prior,
        external_numeric={"fii_cash_net_amount_ratio": 0.1},
    )
    assert features["relative_volume"] > 0
    assert features["cross_section_return_percentile"] > 0
    assert features["fii_cash_net_amount_ratio"] == pytest.approx(0.1)

    with pytest.raises(MetaFeatureError, match="current/future"):
        build_meta_raw_features(
            symbol="S",
            stock_prefix=stock,
            market_prefix=market,
            bank_nifty_prefix=bank,
            india_vix_prefix=vix,
            cross_section=cross,
            sector="Tech",
            prior_stock_sessions=prior + [stock],
        )


def test_institutional_parsing_and_lag_prevents_same_day_leakage():
    ts = int(datetime(2026, 9, 2, 15, 30, tzinfo=INDIA).timestamp() * 1000)
    fii = _parse_fii_payload(
        {
            "status": "success",
            "data": {
                "NSE_EQ|CASH": [{"time_stamp": ts, "buy_amount": 120, "sell_amount": 80}],
                "NSE_FO|INDEX_FUTURES": [{
                    "time_stamp": ts,
                    "buy_amount": 110,
                    "sell_amount": 90,
                    "total_long_contracts": 600,
                    "total_short_contracts": 400,
                }],
            },
        }
    )
    dii = _parse_dii_payload(
        {
            "status": "success",
            "data": {"NSE_EQ|CASH": [{"time_stamp": ts, "buy_amount": 90, "sell_amount": 110}]},
        }
    )
    history = {date(2026, 9, 2): {**fii[date(2026, 9, 2)], **dii[date(2026, 9, 2)]}}
    assert lagged_institutional_features(history, date(2026, 9, 2)) is None
    lagged = lagged_institutional_features(history, date(2026, 9, 3))
    assert lagged is not None
    assert lagged["fii_cash_net_amount_ratio"] > 0
    assert lagged["dii_cash_net_amount_ratio"] < 0


def test_range_stop_is_side_correct_and_conservative():
    start = datetime(2026, 9, 3, 10, 0, tzinfo=INDIA)
    bars = _bars("S", start, n=3, base="100", step="1")
    long_ret = _gross_with_range_stop(
        entry_price=Decimal("100"),
        exit_price=Decimal("103"),
        bars=bars,
        direction=Direction.LONG,
        stop_bps=500.0,
    )
    short_ret = _gross_with_range_stop(
        entry_price=Decimal("100"),
        exit_price=Decimal("103"),
        bars=bars,
        direction=Direction.SHORT,
        stop_bps=500.0,
    )
    assert long_ret == pytest.approx(300.0)
    assert short_ret == pytest.approx(-300.0)


def test_triple_barrier_returns_target_stop_or_time_exit():
    from daybagger.domain import Direction

    start = datetime(2026, 9, 3, 10, 0, tzinfo=INDIA)
    target_bars = _bars("S", start, n=3, base="100", step="1")
    assert _triple_barrier_gross(
        entry_price=Decimal("100"), exit_price=Decimal("103"),
        bars=target_bars, direction=Direction.LONG, stop_bps=100.0,
    ) == pytest.approx(100.0)
    stop_bars = _bars("S", start, n=3, base="100", step="-1")
    assert _triple_barrier_gross(
        entry_price=Decimal("100"), exit_price=Decimal("97"),
        bars=stop_bars, direction=Direction.LONG, stop_bps=100.0,
    ) == pytest.approx(-100.0)


def test_spearman_detects_cross_section_ranking_direction():
    assert _spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert _spearman([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)


def test_live_meta_edge_includes_two_sided_paper_slippage():
    from daybagger.decision.model import ValidatedModelSpec
    from daybagger.meta.forest import ForestRegressorSpec, RegressionTree
    from daybagger.meta.stack import META_CONTEXT_FEATURES, MetaIntelligenceSpec, decide_meta

    raw = {name: 0.0 for name in META_CONTEXT_FEATURES}
    base = ValidatedModelSpec(
        model_id="base_probe",
        version="1",
        direction=Direction.LONG,
        horizon_minutes=30,
        feature_coefficients={"stock_session_return_bps": 0.0},
        bias=0.0,
        favourable_move_bps=10.0,
        adverse_move_bps=10.0,
        validation_id="base-validation",
    )
    # Two leaf-only regressors: LONG predicts +10 bps, SHORT -5 bps.
    leaf_long = RegressionTree(
        children_left=(-1,), children_right=(-1,), feature=(-2,),
        threshold=(-2.0,), value_bps=(10.0,),
    )
    leaf_short = RegressionTree(
        children_left=(-1,), children_right=(-1,), feature=(-2,),
        threshold=(-2.0,), value_bps=(-5.0,),
    )
    feature_name = "base_base_probe_probability"
    long_model = ForestRegressorSpec(
        model_id="meta_long_direct_return", version="meta-v4-direct-return", direction="LONG", horizon_minutes=30,
        feature_names=(feature_name,), trees=(leaf_long,), validation_id="meta-validation",
    )
    short_model = ForestRegressorSpec(
        model_id="meta_short_direct_return", version="meta-v4-direct-return", direction="SHORT", horizon_minutes=30,
        feature_names=(feature_name,), trees=(leaf_short,), validation_id="meta-validation",
    )
    spec = MetaIntelligenceSpec(
        validation_id="meta-validation", version="meta-v4-direct-return", horizon_minutes=30,
        base_specs=(base,), long_model=long_model, short_model=short_model,
        meta_feature_names=(feature_name,), evidence_summary={},
    )
    result = decide_meta(
        spec=spec,
        symbol="AAA",
        as_of=datetime(2026, 9, 3, 11, 0, tzinfo=INDIA),
        raw_features=raw,
        statutory_cost_bps=2.0,
        live_spread_bps=2.0,
        paper_slippage_bps_per_side=2.0,
    )
    assert result.estimated_total_cost_bps == pytest.approx(8.0)
    assert result.opportunity.expected_net_return_bps == pytest.approx(2.0)
    assert result.opportunity.confidence == pytest.approx(1.0)



def test_empty_selection_uses_baseline_brier_not_fake_zero():
    from daybagger.meta.forest import ForestRegressorSpec, RegressionTree
    from daybagger.validation.meta_intelligence import MetaSample, MetaTrainingRow, _evaluate_portfolio

    as_of = datetime(2026, 9, 3, 11, 0, tzinfo=INDIA)
    sample = MetaSample(
        session_date=as_of.date(),
        symbol="AAA",
        as_of=as_of,
        raw_features={"stock_session_range_bps": 50.0},
        entry_price=Decimal("100"),
        long_gross_return_bps=10.0,
        short_gross_return_bps=-10.0,
        long_net_return_bps=-10.0,
        short_net_return_bps=-30.0,
    )
    # Both models predict below the 20 bps cost, therefore zero selected trades.
    tree = RegressionTree(
        children_left=(-1,), children_right=(-1,), feature=(-2,),
        threshold=(-2.0,), value_bps=(5.0,),
    )
    model_long = ForestRegressorSpec(
        model_id="l", version="meta-v4-direct-return", direction="LONG", horizon_minutes=15,
        feature_names=("x",), trees=(tree,), validation_id="v",
    )
    model_short = ForestRegressorSpec(
        model_id="s", version="meta-v4-direct-return", direction="SHORT", horizon_minutes=15,
        feature_names=("x",), trees=(tree,), validation_id="v",
    )
    evidence = _evaluate_portfolio(
        rows=(MetaTrainingRow(sample=sample, meta_features={"x": 0.0}),),
        long_model=model_long,
        short_model=model_short,
        feature_names=("x",),
        cost_bps=20.0,
        horizon_minutes=15,
        starting_capital_inr=Decimal("30000"),
        max_risk_per_trade_inr=Decimal("500"),
        hard_daily_loss_limit_inr=Decimal("1000"),
        max_aggregate_open_risk_inr=Decimal("1000"),
        max_position_fraction=0.5,
    )
    assert evidence.metrics.observations == 0
    assert evidence.metrics.brier_score == pytest.approx(evidence.baseline_brier)
    assert evidence.scored_rows == 1
    assert evidence.positive_edge_rows == 0
    assert evidence.allocation_rejected_rows == 0
    assert evidence.allocation_rejection_reasons == {}


def test_validation_notional_cost_uses_actual_30000_scale():
    from daybagger.integration.costs import IndiaEquityIntradayCostModel

    model = IndiaEquityIntradayCostModel()
    actual = model.round_trip_bps_for_notional(Decimal("30000"))
    conservative_small = model.conservative_linear_round_trip_bps()
    assert actual == pytest.approx(19.28, abs=0.02)
    assert actual < conservative_small


def test_cost_scenarios_include_realistic_position_size_and_spread():
    from daybagger.integration.costs import IndiaEquityIntradayCostModel

    scenarios = IndiaEquityIntradayCostModel().validation_cost_scenarios(
        notionals={"account": Decimal("30000"), "position": Decimal("15000")},
        paper_slippage_bps_per_side=2.0,
    )
    assert scenarios["account"]["spread_0_bps"] == pytest.approx(23.28, abs=0.02)
    assert scenarios["position"]["spread_0_bps"] > scenarios["account"]["spread_0_bps"]
    assert scenarios["position"]["spread_8_bps"] > scenarios["position"]["spread_0_bps"]


def test_validation_family_selection_rejects_unknown_or_empty_sets():
    from pathlib import Path
    from daybagger.validation.meta_intelligence import validate_meta_intelligence

    common = dict(
        repo_root=Path("."),
        access_token="token",
        symbol_to_instrument={"AAA": "key"},
        sector_by_symbol={"AAA": "Tech"},
        institutional_history=None,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 2, 1),
    )
    with pytest.raises(ValueError, match="at least one specialist family"):
        validate_meta_intelligence(**common, family_ids=())
    with pytest.raises(ValueError, match="unknown specialist families"):
        validate_meta_intelligence(**common, family_ids=("unknown",))
