from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

class Direction(StrEnum): LONG='LONG'; SHORT='SHORT'; FLAT='FLAT'
class DecisionStatus(StrEnum): QUALIFIED='QUALIFIED'; REJECTED='REJECTED'; INSUFFICIENT_EVIDENCE='INSUFFICIENT_EVIDENCE'
class ExecutionStatus(StrEnum): REJECTED='REJECTED'; FILLED='FILLED'

@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    symbol: str; as_of: datetime; bid: Decimal; ask: Decimal; last: Decimal|None=None
    def validate(self) -> None:
        if not self.symbol.strip(): raise ValueError('symbol is required')
        if self.as_of.tzinfo is None: raise ValueError('as_of must be timezone-aware')
        if self.bid <= 0 or self.ask <= 0: raise ValueError('bid/ask must be positive')
        if self.ask < self.bid: raise ValueError('ask cannot be below bid')

@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: UUID; source: str; name: str; scope: str; as_of: datetime; value: float|str|bool; quality: float; metadata: Mapping[str,Any]=field(default_factory=dict)
    @classmethod
    def create(cls, *, source:str, name:str, scope:str, as_of:datetime, value:float|str|bool, quality:float, metadata:Mapping[str,Any]|None=None):
        if not 0<=quality<=1: raise ValueError('quality must be between 0 and 1')
        if as_of.tzinfo is None: raise ValueError('as_of must be timezone-aware')
        return cls(uuid4(),source,name,scope,as_of,value,quality,metadata or {})

@dataclass(frozen=True, slots=True)
class ModelOpinion:
    opinion_id: UUID; model_id:str; model_version:str; symbol:str; direction:Direction; as_of:datetime; horizon_minutes:int; probability:float; expected_return_bps:float; evidence_ids:Sequence[UUID]
    @classmethod
    def create(cls, *, model_id:str, model_version:str, symbol:str, direction:Direction, as_of:datetime, horizon_minutes:int, probability:float, expected_return_bps:float, evidence_ids:Sequence[UUID]):
        if direction==Direction.FLAT: raise ValueError('model opinion direction cannot be FLAT')
        if as_of.tzinfo is None: raise ValueError('as_of must be timezone-aware')
        if horizon_minutes<=0: raise ValueError('horizon_minutes must be > 0')
        if not 0<=probability<=1: raise ValueError('probability must be between 0 and 1')
        return cls(uuid4(),model_id,model_version,symbol,direction,as_of,horizon_minutes,probability,expected_return_bps,tuple(evidence_ids))

@dataclass(frozen=True, slots=True)
class Opportunity:
    opportunity_id:UUID; symbol:str; direction:Direction; as_of:datetime; expected_net_return_bps:float; confidence:float; status:DecisionStatus; reason:str; opinion_ids:Sequence[UUID]
    @classmethod
    def create(cls, *, symbol:str, direction:Direction, as_of:datetime, expected_net_return_bps:float, confidence:float, status:DecisionStatus, reason:str, opinion_ids:Sequence[UUID]):
        if as_of.tzinfo is None: raise ValueError('as_of must be timezone-aware')
        if not 0<=confidence<=1: raise ValueError('confidence must be between 0 and 1')
        return cls(uuid4(),symbol,direction,as_of,expected_net_return_bps,confidence,status,reason,tuple(opinion_ids))

@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    request_id:UUID; opportunity_id:UUID; symbol:str; direction:Direction; quantity:int; created_at:datetime
    @classmethod
    def create(cls, *, opportunity_id:UUID, symbol:str, direction:Direction, quantity:int, created_at:datetime):
        if direction==Direction.FLAT: raise ValueError('execution direction cannot be FLAT')
        if quantity<=0: raise ValueError('quantity must be > 0')
        if created_at.tzinfo is None: raise ValueError('created_at must be timezone-aware')
        return cls(uuid4(),opportunity_id,symbol,direction,quantity,created_at)

@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id:UUID; request_id:UUID; status:ExecutionStatus; symbol:str; direction:Direction; quantity:int; filled_price:Decimal|None; executed_at:datetime; reason:str
