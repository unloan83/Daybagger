from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence
from uuid import UUID

from daybagger.domain import Direction, ModelOpinion, Opportunity


@dataclass(frozen=True, slots=True)
class PendingDecisionOutcome:
    trace_id: int
    symbol: str
    instrument_key: str | None
    as_of: datetime
    reference_price: Decimal | None
    estimated_cost_bps: float
    opinions: tuple[ModelOpinion, ...]


class DecisionTraceStore:
    """
    Persist every evaluated decision, including rejected/NO_TRADE opportunities.

    Full opinion payloads are retained so rejected opportunities can later be
    labelled against genuine future candles. The schema migrates older Daybagger
    trace databases in place without destroying their audit history.
    """

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
                    instrument_key TEXT,
                    as_of TEXT NOT NULL,
                    opportunity_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    expected_net_return_bps REAL NOT NULL,
                    confidence REAL NOT NULL,
                    allocation_approved INTEGER NOT NULL,
                    estimated_cost_bps REAL NOT NULL,
                    reference_price TEXT,
                    horizon_minutes INTEGER NOT NULL DEFAULT 0,
                    model_ids_json TEXT NOT NULL,
                    opinions_json TEXT NOT NULL DEFAULT '[]',
                    features_json TEXT NOT NULL DEFAULT '{}',
                    validation_ids_json TEXT NOT NULL DEFAULT '{}',
                    outcome_recorded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            _ensure_columns(conn)
            conn.commit()

    def record_decision(
        self,
        *,
        symbol: str,
        instrument_key: str | None,
        as_of: datetime,
        opportunity: Opportunity,
        allocation_approved: bool,
        estimated_cost_bps: float,
        opinions: Sequence[ModelOpinion],
        validation_ids: Mapping[str, str] | None = None,
        reference_price: Decimal | None = None,
        features: Mapping[str, float] | None = None,
    ) -> int:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if reference_price is not None and reference_price <= 0:
            raise ValueError("reference_price must be positive")
        opinion_tuple = tuple(opinions)
        horizon = max((op.horizon_minutes for op in opinion_tuple), default=0)
        payload = [_opinion_to_dict(op) for op in opinion_tuple]
        model_ids = sorted({op.model_id for op in opinion_tuple})

        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                """
                INSERT INTO decision_traces(
                    created_at_utc, symbol, instrument_key, as_of, opportunity_id,
                    direction, status, reason, expected_net_return_bps, confidence,
                    allocation_approved, estimated_cost_bps, reference_price,
                    horizon_minutes, model_ids_json, opinions_json, features_json,
                    validation_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    instrument_key,
                    as_of.isoformat(),
                    str(opportunity.opportunity_id),
                    opportunity.direction.value,
                    opportunity.status.value,
                    opportunity.reason,
                    opportunity.expected_net_return_bps,
                    opportunity.confidence,
                    int(allocation_approved),
                    estimated_cost_bps,
                    str(reference_price) if reference_price is not None else None,
                    horizon,
                    json.dumps(model_ids, sort_keys=True),
                    json.dumps(payload, sort_keys=True),
                    json.dumps(dict(features or {}), sort_keys=True),
                    json.dumps(dict(validation_ids or {}), sort_keys=True),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def pending_outcomes(self, *, ready_at: datetime) -> list[PendingDecisionOutcome]:
        if ready_at.tzinfo is None:
            raise ValueError("ready_at must be timezone-aware")
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                """
                SELECT id, symbol, instrument_key, as_of, reference_price,
                       estimated_cost_bps, horizon_minutes, opinions_json
                FROM decision_traces
                WHERE outcome_recorded=0 AND horizon_minutes > 0
                ORDER BY as_of, id
                """
            ).fetchall()

        result: list[PendingDecisionOutcome] = []
        for row in rows:
            as_of = datetime.fromisoformat(str(row[3]))
            if as_of.tzinfo is None:
                continue
            horizon = int(row[6])
            if as_of + timedelta(minutes=horizon) > ready_at:
                continue
            try:
                opinions = tuple(
                    _opinion_from_dict(item)
                    for item in json.loads(str(row[7] or "[]"))
                )
            except Exception:
                # Old rows without full opinion payload remain in the audit store but
                # cannot be used for learning. Never fabricate missing predictions.
                continue
            if not opinions:
                continue
            result.append(
                PendingDecisionOutcome(
                    trace_id=int(row[0]),
                    symbol=str(row[1]),
                    instrument_key=str(row[2]) if row[2] else None,
                    as_of=as_of,
                    reference_price=Decimal(str(row[4])) if row[4] else None,
                    estimated_cost_bps=float(row[5]),
                    opinions=opinions,
                )
            )
        return result

    def mark_outcome_recorded(self, trace_id: int) -> None:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "UPDATE decision_traces SET outcome_recorded=1 WHERE id=?",
                (int(trace_id),),
            )
            if cur.rowcount != 1:
                raise ValueError(f"decision trace not found: {trace_id}")
            conn.commit()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(decision_traces)").fetchall()
    }
    additions = {
        "instrument_key": "TEXT",
        "reference_price": "TEXT",
        "horizon_minutes": "INTEGER NOT NULL DEFAULT 0",
        "opinions_json": "TEXT NOT NULL DEFAULT '[]'",
        "features_json": "TEXT NOT NULL DEFAULT '{}'",
        "validation_ids_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    for name, ddl in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE decision_traces ADD COLUMN {name} {ddl}")


def _opinion_to_dict(op: ModelOpinion) -> dict[str, object]:
    return {
        "opinion_id": str(op.opinion_id),
        "model_id": op.model_id,
        "model_version": op.model_version,
        "symbol": op.symbol,
        "direction": op.direction.value,
        "as_of": op.as_of.isoformat(),
        "horizon_minutes": op.horizon_minutes,
        "probability": op.probability,
        "expected_return_bps": op.expected_return_bps,
        "evidence_ids": [str(item) for item in op.evidence_ids],
    }


def _opinion_from_dict(payload: Mapping[str, object]) -> ModelOpinion:
    return ModelOpinion(
        opinion_id=UUID(str(payload["opinion_id"])),
        model_id=str(payload["model_id"]),
        model_version=str(payload["model_version"]),
        symbol=str(payload["symbol"]),
        direction=Direction(str(payload["direction"])),
        as_of=datetime.fromisoformat(str(payload["as_of"])),
        horizon_minutes=int(payload["horizon_minutes"]),
        probability=float(payload["probability"]),
        expected_return_bps=float(payload["expected_return_bps"]),
        evidence_ids=tuple(UUID(str(item)) for item in payload.get("evidence_ids", [])),
    )
