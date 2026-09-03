from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from daybagger.domain import Direction,ExecutableQuote,ExecutionRequest
from daybagger.errors import InvalidMarketDataError
from daybagger.execution.paper import PaperBroker

def req(now,d): return ExecutionRequest.create(opportunity_id=uuid4(),symbol='RELIANCE',direction=d,quantity=2,created_at=now)
def test_long_fills_at_ask():
    now=datetime.now(timezone.utc); q=ExecutableQuote('RELIANCE',now,Decimal('100'),Decimal('100.20')); assert PaperBroker(max_quote_age_seconds=15).execute(request=req(now,Direction.LONG),quote=q,now=now).filled_price==Decimal('100.20')
def test_short_fills_at_bid():
    now=datetime.now(timezone.utc); q=ExecutableQuote('RELIANCE',now,Decimal('100'),Decimal('100.20')); assert PaperBroker(max_quote_age_seconds=15).execute(request=req(now,Direction.SHORT),quote=q,now=now).filled_price==Decimal('100')
def test_stale_quote_is_rejected():
    now=datetime.now(timezone.utc); q=ExecutableQuote('RELIANCE',now-timedelta(seconds=16),Decimal('100'),Decimal('100.20'))
    with pytest.raises(InvalidMarketDataError): PaperBroker(max_quote_age_seconds=15).execute(request=req(now,Direction.LONG),quote=q,now=now)
