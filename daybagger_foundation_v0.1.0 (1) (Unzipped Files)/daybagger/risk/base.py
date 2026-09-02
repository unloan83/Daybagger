from abc import ABC, abstractmethod
from decimal import Decimal
from daybagger.domain import Opportunity, RiskDecision
class RiskEngine(ABC):
    @abstractmethod
    def assess(self, *, opportunity:Opportunity, available_capital_inr:Decimal) -> RiskDecision: raise NotImplementedError
