from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.domain import Direction, ExecutableQuote
from daybagger.runtime.eod import EODReporter
from daybagger.runtime.ledger import PaperLedger


def main() -> int:
    verify_golden_rules(REPO_ROOT)
    now = datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory() as td:
        ledger = PaperLedger(Path(td) / "paper.sqlite3")
        ledger.initialize()

        entry = ExecutableQuote(
            symbol="TEST",
            as_of=now,
            bid=Decimal("99.90"),
            ask=Decimal("100.10"),
            last=Decimal("100.00"),
        )
        pos = ledger.open(
            symbol="TEST",
            direction=Direction.LONG,
            quantity=10,
            quote=entry,
            now=now,
        )
        exit_quote = ExecutableQuote(
            symbol="TEST",
            as_of=now,
            bid=Decimal("101.00"),
            ask=Decimal("101.20"),
            last=Decimal("101.10"),
        )
        ledger.close(
            position_id=pos.position_id,
            quote=exit_quote,
            now=now,
            costs_inr=Decimal("5"),
        )
        summary = EODReporter(ledger).summarize()

    assert summary.open_positions == 0
    assert summary.gross_realised_pnl_inr == Decimal("9.00")
    assert summary.net_realised_pnl_inr == Decimal("4.00")
    print("DAYBAGGER RUNTIME CHECK: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
