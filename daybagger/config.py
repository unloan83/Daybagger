from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import tomllib
from daybagger.errors import ConfigurationError

@dataclass(frozen=True, slots=True)
class AppConfig:
    name: str; environment: str; trading_mode: str; timezone: str; log_level: str
@dataclass(frozen=True, slots=True)
class CapitalConfig:
    starting_capital_inr: float
@dataclass(frozen=True, slots=True)
class StorageConfig:
    control_db_path: str
@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    max_quote_age_seconds: int; paper_slippage_bps: float
@dataclass(frozen=True, slots=True)
class Settings:
    app: AppConfig; capital: CapitalConfig; storage: StorageConfig; execution: ExecutionConfig

def load_settings(path: Path) -> Settings:
    if not path.exists():
        raise ConfigurationError(f"Configuration file not found: {path}")
    with path.open("rb") as fh: raw = tomllib.load(fh)
    try:
        a,c,s,e = raw['app'],raw['capital'],raw['storage'],raw['execution']
        out = Settings(
            app=AppConfig(str(a['name']),str(a['environment']),str(a['trading_mode']).lower(),str(a['timezone']),str(a['log_level']).upper()),
            capital=CapitalConfig(float(c['starting_capital_inr'])),
            storage=StorageConfig(str(s['control_db_path'])),
            execution=ExecutionConfig(int(e['max_quote_age_seconds']),float(e['paper_slippage_bps'])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc
    if out.app.trading_mode != 'paper':
        raise ConfigurationError("Foundation is PAPER ONLY. Set app.trading_mode='paper'.")
    if out.capital.starting_capital_inr <= 0: raise ConfigurationError('starting_capital_inr must be > 0.')
    if out.execution.max_quote_age_seconds <= 0: raise ConfigurationError('max_quote_age_seconds must be > 0.')
    if out.execution.paper_slippage_bps < 0: raise ConfigurationError('paper_slippage_bps cannot be negative.')
    return out
