from abc import ABC, abstractmethod
from datetime import datetime
from typing import Mapping
from uuid import UUID
class OutcomeTracker(ABC):
    @abstractmethod
    def record_observed_outcome(self, *, opportunity_id:UUID, observed_at:datetime, metrics:Mapping[str,float]) -> None: raise NotImplementedError
