from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence


@dataclass(frozen=True, slots=True)
class DatedItem:
    session_date: date
    value: object


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    train: tuple[DatedItem, ...]
    validation: tuple[DatedItem, ...]
    test: tuple[DatedItem, ...]


def chronological_folds(
    items: Sequence[DatedItem],
    *,
    train_sessions: int,
    validation_sessions: int,
    test_sessions: int,
    step_sessions: int | None = None,
) -> list[WalkForwardFold]:
    """
    Strict chronological folds. Test data never influences earlier folds.
    """
    if min(train_sessions, validation_sessions, test_sessions) <= 0:
        raise ValueError("fold sizes must be positive")
    step = step_sessions or test_sessions
    if step <= 0:
        raise ValueError("step_sessions must be positive")

    ordered = sorted(items, key=lambda item: item.session_date)
    total = train_sessions + validation_sessions + test_sessions
    folds: list[WalkForwardFold] = []

    start = 0
    while start + total <= len(ordered):
        a = start + train_sessions
        b = a + validation_sessions
        c = b + test_sessions
        folds.append(
            WalkForwardFold(
                train=tuple(ordered[start:a]),
                validation=tuple(ordered[a:b]),
                test=tuple(ordered[b:c]),
            )
        )
        start += step
    return folds
