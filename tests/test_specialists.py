from daybagger.specialists.catalog import SPECIALIST_FAMILIES
from daybagger.specialists.trainer import TrainingRow, fit_logistic_specialist
from daybagger.domain import Direction


def test_catalog_has_complementary_specialists_without_coefficients():
    assert {"relative_strength", "trend_pullback", "volume_participation", "microstructure", "catalyst"} <= set(SPECIALIST_FAMILIES)
    assert not hasattr(SPECIALIST_FAMILIES["relative_strength"], "threshold")


def test_offline_trainer_exports_unapproved_lightweight_spec():
    family = SPECIALIST_FAMILIES["microstructure"]
    rows = []
    for i in range(20):
        positive = i % 2 == 0
        rows.append(
            TrainingRow(
                features={
                    "spread_bps": 2.0 + i * 0.05,
                    "buy_sell_quantity_imbalance": 0.5 if positive else -0.5,
                    "stock_return_5m_bps": 30 if positive else -30,
                    "stock_vwap_distance_bps": 20 if positive else -20,
                },
                favourable_outcome=positive,
                realised_net_return_bps=25 if positive else -18,
            )
        )

    spec = fit_logistic_specialist(
        family_id="microstructure",
        model_id="micro_long",
        version="1",
        direction=Direction.LONG,
        horizon_minutes=30,
        validation_id="NOT_YET_VALIDATED",
        rows=rows,
    )
    assert spec["approved"] is False
    assert set(spec["feature_coefficients"]) == set(family.required_features)
