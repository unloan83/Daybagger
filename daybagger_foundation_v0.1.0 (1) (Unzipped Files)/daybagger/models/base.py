from abc import ABC, abstractmethod
from datetime import datetime
from typing import Sequence
from daybagger.domain import Evidence, ModelOpinion
class SpecialistModel(ABC):
    model_id:str; model_version:str
    @abstractmethod
    def evaluate(self, *, symbol:str, as_of:datetime, evidence:Sequence[Evidence]) -> ModelOpinion|None: raise NotImplementedError
