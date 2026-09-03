"""Historical evidence, walk-forward validation, and model promotion guard."""

from daybagger.validation.historical import HistoricalCandleClient
from daybagger.validation.metrics import ValidationMetrics, evaluate_predictions
from daybagger.validation.registry import ModelRegistry, PromotionRecord
from daybagger.validation.walkforward import chronological_folds

__all__ = [
    "HistoricalCandleClient",
    "ValidationMetrics",
    "evaluate_predictions",
    "ModelRegistry",
    "PromotionRecord",
    "chronological_folds",
]
