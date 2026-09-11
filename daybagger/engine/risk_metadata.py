from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class RiskMetadataError(RuntimeError):
    """Instrument classification or correlation evidence is unavailable."""


@dataclass(frozen=True, slots=True)
class InstrumentRiskProfile:
    symbol: str
    isin: str
    sector: str


class InstrumentRiskMetadata:
    """Read-only, generated instrument classifications and return correlations."""

    SCHEMA = "daybagger-instrument-risk-metadata-v1"

    def __init__(
        self,
        *,
        profiles: Mapping[str, InstrumentRiskProfile],
        correlations: Mapping[tuple[str, str], float],
        evidence: Mapping[str, object] | None = None,
    ) -> None:
        self._profiles = {key.upper(): value for key, value in profiles.items()}
        self._correlations = {
            self._pair(left, right): float(value)
            for (left, right), value in correlations.items()
        }
        self.evidence = dict(evidence or {})

    @classmethod
    def load(cls, path: str | Path) -> "InstrumentRiskMetadata":
        source = Path(path)
        if not source.exists():
            raise RiskMetadataError(f"risk metadata file is missing: {source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RiskMetadataError(f"risk metadata is unreadable: {source}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != cls.SCHEMA:
            raise RiskMetadataError("unsupported risk metadata schema")

        raw_instruments = payload.get("instruments")
        if not isinstance(raw_instruments, dict) or not raw_instruments:
            raise RiskMetadataError("risk metadata contains no instruments")
        profiles: dict[str, InstrumentRiskProfile] = {}
        for raw_symbol, raw in raw_instruments.items():
            if not isinstance(raw, dict):
                raise RiskMetadataError(f"invalid profile for {raw_symbol}")
            symbol = str(raw_symbol).strip().upper()
            isin = str(raw.get("isin") or "").strip().upper()
            sector = str(raw.get("sector") or "").strip()
            if not symbol or not isin or not sector:
                raise RiskMetadataError(f"incomplete profile for {raw_symbol}")
            profiles[symbol] = InstrumentRiskProfile(symbol, isin, sector)

        correlations: dict[tuple[str, str], float] = {}
        raw_correlations = payload.get("correlations", {})
        if not isinstance(raw_correlations, dict):
            raise RiskMetadataError("correlations must be an object")
        for raw_pair, raw_value in raw_correlations.items():
            names = str(raw_pair).split("|")
            if len(names) != 2:
                raise RiskMetadataError(f"invalid correlation pair: {raw_pair}")
            value = float(raw_value)
            if not -1.0 <= value <= 1.0:
                raise RiskMetadataError(f"invalid correlation value: {raw_pair}")
            correlations[cls._pair(names[0], names[1])] = value

        evidence = {
            key: value
            for key, value in payload.items()
            if key not in {"instruments", "correlations"}
        }
        return cls(profiles=profiles, correlations=correlations, evidence=evidence)

    @classmethod
    def from_profiles(
        cls,
        profiles: Mapping[str, tuple[str, str]],
        correlations: Mapping[tuple[str, str], float] | None = None,
    ) -> "InstrumentRiskMetadata":
        """Test/support constructor; production loads generated JSON evidence."""
        return cls(
            profiles={
                symbol.upper(): InstrumentRiskProfile(
                    symbol.upper(), isin.upper(), sector
                )
                for symbol, (isin, sector) in profiles.items()
            },
            correlations=correlations or {},
            evidence={"schema": cls.SCHEMA, "source": "in-memory"},
        )

    def profile(self, symbol: str) -> InstrumentRiskProfile:
        clean = symbol.strip().upper()
        try:
            return self._profiles[clean]
        except KeyError as exc:
            raise RiskMetadataError(
                f"{clean}: generated sector/correlation metadata unavailable"
            ) from exc

    def correlation(self, left: str, right: str) -> float | None:
        if left.strip().upper() == right.strip().upper():
            return 1.0
        return self._correlations.get(self._pair(left, right))

    @staticmethod
    def _pair(left: str, right: str) -> tuple[str, str]:
        return tuple(sorted((left.strip().upper(), right.strip().upper())))
