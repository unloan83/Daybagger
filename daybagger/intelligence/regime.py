from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from daybagger.intelligence.engine import BreadthFeatures
from daybagger.intelligence.market_context import ContextFeatures


@dataclass(frozen=True, slots=True)
class RegimeEvidence:
    """
    Raw market-regime evidence only.

    No TREND/RANGE/RISK-ON labels are assigned here. That classification
    will be learned/validated later rather than hard-coded now.
    """
    nifty: ContextFeatures
    bank_nifty: ContextFeatures | None
    india_vix: ContextFeatures | None
    breadth: BreadthFeatures | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "nifty": self.nifty.as_dict(),
            "bank_nifty": (
                self.bank_nifty.as_dict() if self.bank_nifty is not None else None
            ),
            "india_vix": (
                self.india_vix.as_dict() if self.india_vix is not None else None
            ),
            "breadth": asdict(self.breadth) if self.breadth is not None else None,
        }
