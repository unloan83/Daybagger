from abc import ABC, abstractmethod
from datetime import datetime
from typing import Sequence
from daybagger.domain import ModelOpinion, Opportunity
class MetaEngine(ABC):
    @abstractmethod
    def rank(self, *, symbol:str, as_of:datetime, opinions:Sequence[ModelOpinion]) -> Opportunity: raise NotImplementedError
