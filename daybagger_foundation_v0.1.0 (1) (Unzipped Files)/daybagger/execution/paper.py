from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from daybagger.domain import Direction, ExecutableQuote, ExecutionRequest, ExecutionResult, ExecutionStatus
from daybagger.errors import InvalidMarketDataError

class PaperBroker:
    def __init__(self, *, max_quote_age_seconds:int, slippage_bps:float=0.0):
        if max_quote_age_seconds<=0: raise ValueError('max_quote_age_seconds must be > 0')
        if slippage_bps<0: raise ValueError('slippage_bps cannot be negative')
        self.max_quote_age_seconds=max_quote_age_seconds; self.slippage_bps=Decimal(str(slippage_bps))
    def execute(self, *, request:ExecutionRequest, quote:ExecutableQuote, now:datetime) -> ExecutionResult:
        quote.validate()
        if now.tzinfo is None: raise InvalidMarketDataError('now must be timezone-aware')
        if request.symbol!=quote.symbol: raise InvalidMarketDataError('request/quote symbol mismatch')
        age=(now-quote.as_of).total_seconds()
        if age<0: raise InvalidMarketDataError('quote timestamp is in the future')
        if age>self.max_quote_age_seconds: raise InvalidMarketDataError(f'stale quote: age={age:.3f}s > {self.max_quote_age_seconds}s')
        if request.direction==Direction.LONG:
            base=quote.ask; fill=base + base*self.slippage_bps/Decimal('10000')
        elif request.direction==Direction.SHORT:
            base=quote.bid; fill=base - base*self.slippage_bps/Decimal('10000')
        else:
            return ExecutionResult(uuid4(),request.request_id,ExecutionStatus.REJECTED,request.symbol,request.direction,request.quantity,None,now,'FLAT_DIRECTION_NOT_EXECUTABLE')
        return ExecutionResult(uuid4(),request.request_id,ExecutionStatus.FILLED,request.symbol,request.direction,request.quantity,fill,now,'PAPER_FILL_FROM_EXECUTABLE_QUOTE')
