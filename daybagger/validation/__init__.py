"""Historical evidence, walk-forward validation, and model promotion guard.

Imports stay lazy so standalone, read-only evidence builders do not initialize
unrelated live-domain modules or database drivers.
"""

__all__ = [
    "HistoricalCandleClient",
    "ValidationMetrics",
    "evaluate_predictions",
    "ModelRegistry",
    "PromotionRecord",
    "chronological_folds",
]


def __getattr__(name):
    if name == "HistoricalCandleClient":
        from daybagger.validation.historical import HistoricalCandleClient
        return HistoricalCandleClient
    if name in {"ValidationMetrics", "evaluate_predictions"}:
        from daybagger.validation.metrics import ValidationMetrics, evaluate_predictions
        return {"ValidationMetrics": ValidationMetrics, "evaluate_predictions": evaluate_predictions}[name]
    if name in {"ModelRegistry", "PromotionRecord"}:
        from daybagger.validation.registry import ModelRegistry, PromotionRecord
        return {"ModelRegistry": ModelRegistry, "PromotionRecord": PromotionRecord}[name]
    if name == "chronological_folds":
        from daybagger.validation.walkforward import chronological_folds
        return chronological_folds
    raise AttributeError(name)
