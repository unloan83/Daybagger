from __future__ import annotations
import tempfile, sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
REPO_ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(REPO_ROOT))
from daybagger.bootstrap import verify_golden_rules
from daybagger.config import load_settings
from daybagger.domain import Direction, ExecutableQuote, ExecutionRequest
from daybagger.execution.paper import PaperBroker
from daybagger.storage.sqlite_store import SQLiteControlStore

def main()->int:
    rules=verify_golden_rules(REPO_ROOT); settings=load_settings(REPO_ROOT/'config'/'default.toml'); now=datetime.now(timezone.utc)
    quote=ExecutableQuote('TEST',now,Decimal('99.90'),Decimal('100.10'),Decimal('100.00'))
    request=ExecutionRequest.create(opportunity_id=uuid4(),symbol='TEST',direction=Direction.LONG,quantity=1,created_at=now)
    result=PaperBroker(max_quote_age_seconds=settings.execution.max_quote_age_seconds,slippage_bps=settings.execution.paper_slippage_bps).execute(request=request,quote=quote,now=now)
    with tempfile.TemporaryDirectory() as td:
        store=SQLiteControlStore(Path(td)/'control.sqlite3'); store.initialize(); store.record_event('SELF_CHECK',{'status':'PASS'}); assert store.recent_events(1)[0]['payload']['status']=='PASS'
    expected_fill = quote.ask * (Decimal('1') + Decimal(str(settings.execution.paper_slippage_bps)) / Decimal('10000'))
    assert result.filled_price == expected_fill
    print(f'DAYBAGGER FOUNDATION CHECK: PASS (goldenrules_sha256={rules.sha256[:12]}..., mode={settings.app.trading_mode})'); return 0
if __name__=='__main__': raise SystemExit(main())
