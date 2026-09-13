from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping, Sequence


IMDS_INSTANCE_URL = "http://169.254.169.254/opc/v2/instance/"
_UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@:-]+$")


class BillingGuardError(RuntimeError):
    """The guard could not establish a trustworthy billing state."""


@dataclass(frozen=True, slots=True)
class GuardConfig:
    threshold_amount: Decimal
    expected_currency: str
    enforcement_enabled: bool
    stop_instance_on_breach: bool
    fail_closed_on_api_error: bool
    stop_instance_on_api_error: bool
    lookback_hours: int
    state_dir: Path
    workload_units: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CostLine:
    amount: Decimal
    currency: str
    resource_id: str | None
    service: str | None
    sku_name: str | None


@dataclass(frozen=True, slots=True)
class CostEvaluation:
    positive_amount: Decimal
    currency: str
    threshold_amount: Decimal
    breached: bool
    positive_lines: tuple[CostLine, ...]


def _parse_bool(value: str, *, key: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BillingGuardError(f"{key} must be true or false")


def read_env_file(path: Path) -> dict[str, str]:
    """Read a data-only KEY=value file without evaluating shell syntax."""
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BillingGuardError(f"invalid config line {number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise BillingGuardError(f"invalid config key on line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_config(path: Path) -> GuardConfig:
    values = read_env_file(path)
    try:
        threshold = Decimal(values.get("THRESHOLD_AMOUNT", "0.01"))
    except InvalidOperation as exc:
        raise BillingGuardError("THRESHOLD_AMOUNT must be decimal") from exc
    if threshold <= 0:
        raise BillingGuardError("THRESHOLD_AMOUNT must be greater than zero")

    currency = values.get("EXPECTED_CURRENCY", "USD").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise BillingGuardError("EXPECTED_CURRENCY must be a three-letter code")

    try:
        lookback_hours = int(values.get("LOOKBACK_HOURS", "36"))
    except ValueError as exc:
        raise BillingGuardError("LOOKBACK_HOURS must be an integer") from exc
    if not 1 <= lookback_hours <= 36:
        raise BillingGuardError("LOOKBACK_HOURS must be between 1 and 36")

    units = tuple(values.get("WORKLOAD_UNITS", "daybagger-research.slice").split())
    if not units or any(not _UNIT_NAME.fullmatch(unit) for unit in units):
        raise BillingGuardError("WORKLOAD_UNITS contains an invalid systemd unit")

    return GuardConfig(
        threshold_amount=threshold,
        expected_currency=currency,
        enforcement_enabled=_parse_bool(
            values.get("ENFORCEMENT_ENABLED", "false"), key="ENFORCEMENT_ENABLED"
        ),
        stop_instance_on_breach=_parse_bool(
            values.get("STOP_INSTANCE_ON_BREACH", "true"),
            key="STOP_INSTANCE_ON_BREACH",
        ),
        fail_closed_on_api_error=_parse_bool(
            values.get("FAIL_CLOSED_ON_API_ERROR", "true"),
            key="FAIL_CLOSED_ON_API_ERROR",
        ),
        stop_instance_on_api_error=_parse_bool(
            values.get("STOP_INSTANCE_ON_API_ERROR", "false"),
            key="STOP_INSTANCE_ON_API_ERROR",
        ),
        lookback_hours=lookback_hours,
        state_dir=Path(values.get("STATE_DIR", "/var/lib/daybagger-billing-guard")),
        workload_units=units,
    )


def _field(item: object, name: str) -> object | None:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def evaluate_costs(
    items: Iterable[object], *, threshold: Decimal, expected_currency: str
) -> CostEvaluation:
    positive: list[CostLine] = []
    observed_currencies: set[str] = set()
    total = Decimal("0")

    for item in items:
        raw_amount = _field(item, "computed_amount")
        if raw_amount is None:
            continue
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation as exc:
            raise BillingGuardError("OCI returned a non-decimal computed amount") from exc
        currency = str(_field(item, "currency") or expected_currency).upper()
        if amount > 0:
            observed_currencies.add(currency)
            total += amount
            positive.append(
                CostLine(
                    amount=amount,
                    currency=currency,
                    resource_id=_as_optional_string(_field(item, "resource_id")),
                    service=_as_optional_string(_field(item, "service")),
                    sku_name=_as_optional_string(_field(item, "sku_name")),
                )
            )

    unexpected = observed_currencies - {expected_currency}
    if unexpected:
        raise BillingGuardError(
            "OCI returned unexpected currencies: " + ",".join(sorted(unexpected))
        )

    return CostEvaluation(
        positive_amount=total,
        currency=expected_currency,
        threshold_amount=threshold,
        breached=total >= threshold,
        positive_lines=tuple(positive),
    )


def _as_optional_string(value: object | None) -> str | None:
    return None if value is None else str(value)


def read_instance_metadata() -> dict[str, object]:
    request = urllib.request.Request(
        IMDS_INSTANCE_URL, headers={"Authorization": "Bearer Oracle"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.load(response)
    for required in ("id", "compartmentId", "region"):
        if not data.get(required):
            raise BillingGuardError(f"instance metadata missing {required}")
    return data


def query_recent_costs(
    config: GuardConfig, *, now: datetime | None = None
) -> tuple[CostEvaluation, object, dict[str, object]]:
    try:
        import oci
    except ImportError as exc:
        raise BillingGuardError("OCI Python SDK is not installed") from exc

    metadata = read_instance_metadata()
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.usage_api.UsageapiClient(
        {"region": str(metadata["region"])}, signer=signer
    )

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = current.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=config.lookback_hours)
    details = oci.usage_api.models.RequestSummarizedUsagesDetails(
        tenant_id=signer.tenancy_id,
        time_usage_started=start,
        time_usage_ended=end,
        granularity="HOURLY",
        is_aggregate_by_time=True,
        query_type="COST",
        group_by=["resourceId", "service", "skuName"],
    )

    items: list[object] = []
    page: str | None = None
    while True:
        response = client.request_summarized_usages(
            details,
            page=page,
            limit=1000,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
        )
        items.extend(response.data.items)
        page = response.headers.get("opc-next-page")
        if not page:
            break

    return (
        evaluate_costs(
            items,
            threshold=config.threshold_amount,
            expected_currency=config.expected_currency,
        ),
        signer,
        metadata,
    )


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def lock_down(
    config: GuardConfig, *, reason: str, details: Mapping[str, object]
) -> list[str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    _atomic_json(
        config.state_dir / "LOCKDOWN.json",
        {"locked_at": timestamp, "reason": reason, "details": dict(details)},
    )
    actions = ["lockdown-written"]
    for unit in config.workload_units:
        result = subprocess.run(
            ["systemctl", "stop", unit], check=False, capture_output=True, text=True
        )
        actions.append(f"stop:{unit}:rc={result.returncode}")
    return actions


def stop_this_instance(*, signer: object, metadata: Mapping[str, object]) -> None:
    import oci

    client = oci.core.ComputeClient(
        {"region": str(metadata["region"])}, signer=signer
    )
    client.instance_action(str(metadata["id"]), "STOP")


def _evaluation_payload(evaluation: CostEvaluation) -> dict[str, object]:
    return {
        "positive_amount": str(evaluation.positive_amount),
        "currency": evaluation.currency,
        "threshold_amount": str(evaluation.threshold_amount),
        "breached": evaluation.breached,
        "positive_lines": [
            {**asdict(line), "amount": str(line.amount)}
            for line in evaluation.positive_lines
        ],
    }


def run(config: GuardConfig, *, preflight: bool = False) -> int:
    try:
        evaluation, signer, metadata = query_recent_costs(config)
    except Exception as exc:
        payload: dict[str, object] = {
            "status": "API_ERROR",
            "error_type": type(exc).__name__,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if config.enforcement_enabled and config.fail_closed_on_api_error and not preflight:
            payload["actions"] = lock_down(
                config, reason="billing-api-error", details=payload
            )
            if config.stop_instance_on_api_error:
                try:
                    metadata = read_instance_metadata()
                    import oci

                    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
                    stop_this_instance(signer=signer, metadata=metadata)
                    payload["instance_stop"] = "requested"
                except Exception as stop_exc:
                    payload["instance_stop"] = f"failed:{type(stop_exc).__name__}"
        print(json.dumps(payload, sort_keys=True))
        return 3

    payload = {
        "status": "BREACH" if evaluation.breached else "OK",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **_evaluation_payload(evaluation),
    }
    _atomic_json(config.state_dir / "last-check.json", payload)

    if preflight:
        payload["status"] = "PREFLIGHT_OK"
        print(json.dumps(payload, sort_keys=True))
        return 0

    if not evaluation.breached:
        print(json.dumps(payload, sort_keys=True))
        return 0

    if not config.enforcement_enabled:
        payload["status"] = "BREACH_NOT_ENFORCED"
        print(json.dumps(payload, sort_keys=True))
        return 2

    payload["actions"] = lock_down(config, reason="positive-cost", details=payload)
    print(json.dumps(payload, sort_keys=True), flush=True)
    if config.stop_instance_on_breach:
        stop_this_instance(signer=signer, metadata=metadata)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed OCI billing guard")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/daybagger/billing-guard.env"),
    )
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        return run(config, preflight=args.preflight)
    except (BillingGuardError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "CONFIG_ERROR", "error_type": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
