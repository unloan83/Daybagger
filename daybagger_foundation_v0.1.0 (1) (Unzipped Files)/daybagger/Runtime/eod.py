from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from daybagger.runtime.ledger import PaperLedger


@dataclass(frozen=True, slots=True)
class EODSummary:
    open_positions: int
    gross_realised_pnl_inr: Decimal
    net_realised_pnl_inr: Decimal | None
    costs_complete: bool


class EODReporter:
    def __init__(self, ledger: PaperLedger):
        self.ledger = ledger

    def summarize(self) -> EODSummary:
        gross, net = self.ledger.realised_pnl()
        return EODSummary(
            open_positions=len(self.ledger.open_positions()),
            gross_realised_pnl_inr=gross,
            net_realised_pnl_inr=net,
            costs_complete=(net is not None),
        )
