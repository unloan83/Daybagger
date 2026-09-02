from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo


class SessionState(StrEnum):
    PREMARKET = "PREMARKET"
    MARKET = "MARKET"
    ENTRY_CLOSED = "ENTRY_CLOSED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class SessionGuard:
    """
    Simple Indian cash-market session guard.

    Times are configuration inputs. No strategy lives here.
    """
    timezone: str = "Asia/Kolkata"
    market_open: time = time(9, 15)
    entry_cutoff: time = time(15, 5)
    mandatory_exit: time = time(15, 10)
    market_close: time = time(15, 30)

    def state(self, now: datetime) -> SessionState:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        local = now.astimezone(ZoneInfo(self.timezone)).time()

        if local < self.market_open:
            return SessionState.PREMARKET
        if local < self.entry_cutoff:
            return SessionState.MARKET
        if local < self.mandatory_exit:
            return SessionState.ENTRY_CLOSED
        return SessionState.CLOSED

    def entries_allowed(self, now: datetime) -> bool:
        return self.state(now) == SessionState.MARKET
