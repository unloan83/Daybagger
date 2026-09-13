from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from daybagger.operations.oci_billing_guard import (
    BillingGuardError,
    evaluate_costs,
    load_config,
    read_env_file,
)


def test_zero_cost_is_safe() -> None:
    result = evaluate_costs(
        [SimpleNamespace(computed_amount=0, currency="USD")],
        threshold=Decimal("0.01"),
        expected_currency="USD",
    )
    assert result.positive_amount == 0
    assert not result.breached


def test_cost_at_threshold_breaches() -> None:
    result = evaluate_costs(
        [
            SimpleNamespace(
                computed_amount="0.004",
                currency="USD",
                resource_id="one",
                service="Compute",
                sku_name="A",
            ),
            SimpleNamespace(
                computed_amount="0.006",
                currency="USD",
                resource_id="two",
                service="Block Storage",
                sku_name="B",
            ),
        ],
        threshold=Decimal("0.01"),
        expected_currency="USD",
    )
    assert result.positive_amount == Decimal("0.010")
    assert result.breached
    assert len(result.positive_lines) == 2


def test_credits_do_not_hide_positive_cost_lines() -> None:
    result = evaluate_costs(
        [
            {"computed_amount": "0.02", "currency": "USD"},
            {"computed_amount": "-0.02", "currency": "USD"},
        ],
        threshold=Decimal("0.01"),
        expected_currency="USD",
    )
    assert result.positive_amount == Decimal("0.02")
    assert result.breached


def test_unexpected_positive_currency_fails_closed() -> None:
    with pytest.raises(BillingGuardError, match="unexpected currencies"):
        evaluate_costs(
            [{"computed_amount": "1", "currency": "INR"}],
            threshold=Decimal("0.01"),
            expected_currency="USD",
        )


def test_env_parser_does_not_execute_shell(tmp_path: Path) -> None:
    config = tmp_path / "guard.env"
    config.write_text(
        "# data only\nEXPECTED_CURRENCY='USD'\nVALUE=$(touch /tmp/not-executed)\n",
        encoding="utf-8",
    )
    values = read_env_file(config)
    assert values["EXPECTED_CURRENCY"] == "USD"
    assert values["VALUE"] == "$(touch /tmp/not-executed)"


def test_config_defaults_to_non_enforcing(tmp_path: Path) -> None:
    path = tmp_path / "guard.env"
    path.write_text("EXPECTED_CURRENCY=USD\n", encoding="utf-8")
    config = load_config(path)
    assert not config.enforcement_enabled
    assert config.fail_closed_on_api_error
    assert config.stop_instance_on_breach
    assert not config.stop_instance_on_api_error


@pytest.mark.parametrize("hours", [0, 37])
def test_invalid_lookback_rejected(tmp_path: Path, hours: int) -> None:
    path = tmp_path / "guard.env"
    path.write_text(f"LOOKBACK_HOURS={hours}\n", encoding="utf-8")
    with pytest.raises(BillingGuardError, match="LOOKBACK_HOURS"):
        load_config(path)
