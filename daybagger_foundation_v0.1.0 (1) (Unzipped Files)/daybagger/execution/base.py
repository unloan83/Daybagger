from abc import ABC, abstractmethod
from datetime import datetime
from daybagger.domain import ExecutableQuote, ExecutionRequest, ExecutionResult
class Broker(ABC):
    @abstractmethod
    def execute(self, *, request:ExecutionRequest, quote:ExecutableQuote, now:datetime) -> ExecutionResult: raise NotImplementedError
