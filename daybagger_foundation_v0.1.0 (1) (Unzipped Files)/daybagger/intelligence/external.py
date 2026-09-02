from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ExternalEvidence:
    """
    Standard contract for future FREE external intelligence.

    Examples:
    - global index / GIFT NIFTY context
    - corporate announcements
    - bulk/block deals
    - FII/DII flows
    - F&O/OI context
    - scheduled macro events
    - news/event evidence

    This file intentionally contains no network client yet. We reuse official/free
    sources when each source is added rather than inventing a generic scraper.
    """
    source: str
    category: str
    as_of: datetime
    value: float | str | bool
    quality: float
    metadata: Mapping[str, Any]

    def validate(self) -> None:
        if not self.source.strip():
            raise ValueError("source is required")
        if not self.category.strip():
            raise ValueError("category is required")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if not 0.0 <= self.quality <= 1.0:
            raise ValueError("quality must be between 0 and 1")
