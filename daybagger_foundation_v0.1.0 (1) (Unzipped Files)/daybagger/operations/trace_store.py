from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from daybagger.integration.engine import DecisionTrace


class DecisionTraceStore:
    """Persists every evaluated decision, including NO_TRADE/rejections."""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    opportunity_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    expected_net_return_bps REAL NOT NULL,
                    confidence REAL NOT NULL,
                    allocation_approved INTEGER NOT NULL,
                    estimated_cost_bps REAL NOT NULL,
                    model_ids_json TEXT NOT NULL,
                    outcome_recorded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def record(self, trace: DecisionTrace) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                """
                INSERT INTO decision_traces(
                    created_at_utc, symbol, as_of, opportunity_id, direction,
                    status, reason, expected_net_return_bps, confidence,
                    allocation_approved, estimated_cost_bps, model_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    trace.symbol,
                    trace.as_of.isoformat(),
                    str(trace.opportunity.opportunity_id),
                    trace.opportunity.direction.value,
                    trace.opportunity.status.value,
                    trace.opportunity.reason,
                    trace.opportunity.expected_net_return_bps,
                    trace.opportunity.confidence,
                    int(trace.allocation.approved),
                    trace.estimated_cost_bps,
                    json.dumps([op.model_id for op in trace.opinions], sort_keys=True),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
