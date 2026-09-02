from abc import ABC, abstractmethod
from datetime import datetime
from typing import Sequence
from daybagger.domain import Evidence
class IntelligenceSource(ABC):
    source_id: str
    @abstractmethod
    def collect(self, *, symbol:str, as_of:datetime) -> Sequence[Evidence]: raise NotImplementedError
