from abc import ABC, abstractmethod
from datetime import datetime
from daybagger.domain import ExecutableQuote
class MarketDataProvider(ABC):
    @abstractmethod
    def executable_quote(self, symbol:str, as_of:datetime) -> ExecutableQuote: raise NotImplementedError
