from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """
    Canonical replay record.

    Stable hashes let Daybagger prove that the same timestamped features +
    model versions produce the same decision in replay/backtest/paper runtime.
    """
    as_of_iso: str
    symbol: str
    feature_payload: Mapping[str, Any]
    model_versions: Mapping[str, str]
    decision_payload: Mapping[str, Any]

    def stable_hash(self) -> str:
        raw = json.dumps(
            {
                "as_of_iso": self.as_of_iso,
                "symbol": self.symbol,
                "feature_payload": self.feature_payload,
                "model_versions": self.model_versions,
                "decision_payload": self.decision_payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
