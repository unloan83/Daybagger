from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from daybagger.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class AppConfig:
    name: str
    environment: str
    trading_mode: str
    timezone: str
    log_level: str


@dataclass(frozen=True, slots=True)
class CapitalConfig:
    starting_capital_inr: float


@dataclass(frozen=True, slots=True)
class StorageConfig:
    control_db_path: str
    paper_ledger_path: str
    decision_trace_path: str
    learning_db_path: str


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    max_quote_age_seconds: int
    paper_slippage_bps: float


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_risk_per_trade_inr: float
    hard_daily_loss_limit_inr: float
    max_aggregate_open_risk_inr: float
    max_position_fraction: float


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    deep_scan_symbols: int
    cycle_seconds: int
    learning_min_observations: int
    learning_lookback_days: int


@dataclass(frozen=True, slots=True)
class Settings:
    app: AppConfig
    capital: CapitalConfig
    storage: StorageConfig
    execution: ExecutionConfig
    risk: RiskConfig
    runtime: RuntimeConfig


def load_settings(path: Path) -> Settings:
    if not path.exists():
        raise ConfigurationError(f"Configuration file not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    try:
        a, c, s, e = raw["app"], raw["capital"], raw["storage"], raw["execution"]
        r = raw.get("risk", {})
        rt = raw.get("runtime", {})
        out = Settings(
            app=AppConfig(
                str(a["name"]),
                str(a["environment"]),
                str(a["trading_mode"]).lower(),
                str(a["timezone"]),
                str(a["log_level"]).upper(),
            ),
            capital=CapitalConfig(float(c["starting_capital_inr"])),
            storage=StorageConfig(
                str(s["control_db_path"]),
                str(s.get("paper_ledger_path", "data/paper.sqlite3")),
                str(s.get("decision_trace_path", "data/decision_traces.sqlite3")),
                str(s.get("learning_db_path", "data/learning.sqlite3")),
            ),
            execution=ExecutionConfig(
                int(e["max_quote_age_seconds"]),
                float(e["paper_slippage_bps"]),
            ),
            risk=RiskConfig(
                float(r.get("max_risk_per_trade_inr", 500.0)),
                float(r.get("hard_daily_loss_limit_inr", 1000.0)),
                float(r.get("max_aggregate_open_risk_inr", 1000.0)),
                float(r.get("max_position_fraction", 0.5)),
            ),
            runtime=RuntimeConfig(
                int(rt.get("deep_scan_symbols", 40)),
                int(rt.get("cycle_seconds", 300)),
                int(rt.get("learning_min_observations", 20)),
                int(rt.get("learning_lookback_days", 90)),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc

    if out.app.trading_mode != "paper":
        raise ConfigurationError("Foundation is PAPER ONLY. Set app.trading_mode='paper'.")
    if out.capital.starting_capital_inr <= 0:
        raise ConfigurationError("starting_capital_inr must be > 0.")
    if out.execution.max_quote_age_seconds <= 0:
        raise ConfigurationError("max_quote_age_seconds must be > 0.")
    if out.execution.paper_slippage_bps < 0:
        raise ConfigurationError("paper_slippage_bps cannot be negative.")
    if out.risk.max_risk_per_trade_inr <= 0:
        raise ConfigurationError("max_risk_per_trade_inr must be > 0.")
    if out.risk.hard_daily_loss_limit_inr <= 0:
        raise ConfigurationError("hard_daily_loss_limit_inr must be > 0.")
    if out.risk.max_aggregate_open_risk_inr <= 0:
        raise ConfigurationError("max_aggregate_open_risk_inr must be > 0.")
    if not 0 < out.risk.max_position_fraction <= 1:
        raise ConfigurationError("max_position_fraction must be in (0,1].")
    if not 6 <= out.runtime.deep_scan_symbols <= 100:
        raise ConfigurationError("deep_scan_symbols must be between 6 and 100.")
    if out.runtime.cycle_seconds < 60:
        raise ConfigurationError("cycle_seconds must be >= 60.")
    if out.runtime.learning_min_observations < 2:
        raise ConfigurationError("learning_min_observations must be >= 2.")
    if out.runtime.learning_lookback_days <= 0:
        raise ConfigurationError("learning_lookback_days must be > 0.")
    return out
