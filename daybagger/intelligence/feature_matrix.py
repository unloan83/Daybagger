from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from daybagger.intelligence.engine import (
    MicrostructureFeatures,
    RelativeStrengthFeatures,
    TimeNormalizedVolumeFeatures,
)


@dataclass(frozen=True, slots=True)
class StockFeatureRow:
    """
    One timestamp-aligned feature row for future specialist/meta models.

    No score or trade decision is stored here.
    """
    symbol: str
    instrument_key: str
    as_of_iso: str
    market_regime: Mapping[str, Any]
    sector_context: Mapping[str, Any] | None
    relative_strength: RelativeStrengthFeatures
    microstructure: MicrostructureFeatures
    volume: TimeNormalizedVolumeFeatures | None
    extra: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "instrument_key": self.instrument_key,
            "as_of_iso": self.as_of_iso,
            "market_regime": dict(self.market_regime),
            "sector_context": (
                dict(self.sector_context) if self.sector_context is not None else None
            ),
            "relative_strength": asdict(self.relative_strength),
            "microstructure": asdict(self.microstructure),
            "volume": asdict(self.volume) if self.volume is not None else None,
            "extra": dict(self.extra),
        }
