import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from app.operations_models import GapStatus, KnowledgeGap, Outcome, Reason
from app.operations_privacy import question_fingerprint, redact_question


GapFilter = GapStatus | Literal["all"]
Window = Literal["7d", "30d", "all"]

_OUTCOMES = {
    "replied",
    "handed_off",
    "ignored_human_mode",
    "duplicate",
    "failed",
}
_HANDOFF_REASONS = {
    "explicit_human_request",
    "complaint_or_legal",
    "payment_or_account_security",
    "knowledge_gap",
    "dify_error",
}
_FAILURE_REASONS = {"chatwoot_error", "internal_error"}
_GAP_STATUSES = {"pending", "resolved", "ignored"}


class OperationsStore:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def record_event(
        self,
        *,
        message_id: int,
        conversation_id: int,
        outcome: Outcome,
        reason: Reason | None,
        duration_ms: int,
        created_at: int | None = None,
    ) -> None:
        _require_positive_id(message_id, "Message ID")
        _require_positive_id(conversation_id, "Conversation ID")
        _validate_event(outcome, reason)
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError("Duration must be a non-negative integer.")
        timestamp = _timestamp(created_at)

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO service_events (
                    message_id, conversation_id, outcome, reason,
                    duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    outcome,
                    reason,
                    duration_ms,
                    timestamp,
                ),
            )

    def record_knowledge_gap(
        self,
        *,
        question: str,
        conversation_id: int,
        created_at: int | None = None,
    ) -> None:
        _require_positive_id(conversation_id, "Conversation ID")
        if not isinstance(question, str):
            raise ValueError("Question must be text.")
        sample_question = redact_question(question)
        if not sample_question:
            raise ValueError("Question must not be empty.")
        timestamp = _timestamp(created_at)
        fingerprint = question_fingerprint(question)

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_gaps (
                    fingerprint, sample_question, occurrence_count,
                    first_seen_at, last_seen_at, last_conversation_id,
                    status, note, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, 'pending', '', ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    occurrence_count = knowledge_gaps.occurrence_count + 1,
                    last_seen_at = excluded.last_seen_at,
                    last_conversation_id = excluded.last_conversation_id
                """,
                (
                    fingerprint,
                    sample_question,
                    timestamp,
                    timestamp,
                    conversation_id,
                    timestamp,
                ),
            )

    def list_gaps(
        self,
        *,
        status: GapFilter = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[KnowledgeGap], int]:
        if status not in _GAP_STATUSES | {"all"}:
            raise ValueError("Invalid knowledge-gap status.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Limit must be between 1 and 200.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Offset must be a non-negative integer.")

        where = "" if status == "all" else "WHERE status = ?"
        parameters: tuple[object, ...] = () if status == "all" else (status,)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM knowledge_gaps {where}",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT id, fingerprint, sample_question, occurrence_count,
                       first_seen_at, last_seen_at, last_conversation_id,
                       status, note, updated_at
                FROM knowledge_gaps
                {where}
                ORDER BY occurrence_count DESC, last_seen_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        return ([_row_to_gap(row) for row in rows], total)

    def update_gap(
        self,
        gap_id: int,
        *,
        status: GapStatus,
        note: str,
        updated_at: int | None = None,
    ) -> KnowledgeGap | None:
        _require_positive_id(gap_id, "Knowledge-gap ID")
        if status not in _GAP_STATUSES:
            raise ValueError("Invalid knowledge-gap status.")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("Knowledge-gap note must be at most 1000 characters.")
        timestamp = _timestamp(updated_at)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_gaps
                SET status = ?, note = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, note, timestamp, gap_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                """
                SELECT id, fingerprint, sample_question, occurrence_count,
                       first_seen_at, last_seen_at, last_conversation_id,
                       status, note, updated_at
                FROM knowledge_gaps
                WHERE id = ?
                """,
                (gap_id,),
            ).fetchone()
        return _row_to_gap(row)

    def get_summary(
        self,
        window: Window,
        *,
        now: int | None = None,
    ) -> dict[str, object]:
        cutoff = _window_cutoff(window, now)
        with self._connect() as connection:
            final_events = self._final_events(connection, cutoff)
            duplicate_webhooks = self._duplicate_count(connection, cutoff)

        replied = sum(row["outcome"] == "replied" for row in final_events)
        handed_off = sum(row["outcome"] == "handed_off" for row in final_events)
        ignored_human_mode = sum(
            row["outcome"] == "ignored_human_mode" for row in final_events
        )
        failed = sum(row["outcome"] == "failed" for row in final_events)
        ai_eligible = replied + handed_off
        knowledge_gaps = sum(
            row["reason"] == "knowledge_gap" for row in final_events
        )
        system_errors = sum(
            row["reason"] == "dify_error" or row["outcome"] == "failed"
            for row in final_events
        )
        ai_durations = [
            int(row["duration_ms"])
            for row in final_events
            if row["outcome"] in {"replied", "handed_off"}
        ]
        average_duration = (
            round(sum(ai_durations) / len(ai_durations), 2)
            if ai_durations
            else 0
        )
        handoff_reasons = {reason: 0 for reason in sorted(_HANDOFF_REASONS)}
        for row in final_events:
            reason = row["reason"]
            if row["outcome"] == "handed_off" and reason in handoff_reasons:
                handoff_reasons[str(reason)] += 1

        return {
            "window": window,
            "unique_customer_messages": len(final_events),
            "ai_eligible_messages": ai_eligible,
            "replied": replied,
            "handed_off": handed_off,
            "ignored_human_mode": ignored_human_mode,
            "failed": failed,
            "system_errors": system_errors,
            "duplicate_webhooks": duplicate_webhooks,
            "average_ai_duration_ms": average_duration,
            "answer_rate": _rate(replied, ai_eligible),
            "handoff_rate": _rate(handed_off, ai_eligible),
            "knowledge_gap_rate": _rate(knowledge_gaps, ai_eligible),
            "handoff_reasons": handoff_reasons,
            "trend": _daily_trend(final_events),
        }

    def export_events(
        self,
        window: Window,
        *,
        now: int | None = None,
    ) -> list[dict[str, object]]:
        cutoff = _window_cutoff(window, now)
        where = "" if cutoff is None else "WHERE created_at >= ?"
        parameters: tuple[object, ...] = () if cutoff is None else (cutoff,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, message_id, conversation_id, outcome, reason,
                       duration_ms, created_at
                FROM service_events
                {where}
                ORDER BY created_at DESC, id DESC
                """,
                parameters,
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "message_id": int(row["message_id"]),
                "conversation_id": int(row["conversation_id"]),
                "outcome": str(row["outcome"]),
                "reason": row["reason"],
                "duration_ms": int(row["duration_ms"]),
                "created_at": int(row["created_at"]),
            }
            for row in rows
        ]

    def export_gaps(
        self,
        window: Window,
        *,
        now: int | None = None,
    ) -> list[dict[str, object]]:
        cutoff = _window_cutoff(window, now)
        where = "" if cutoff is None else "WHERE last_seen_at >= ?"
        parameters: tuple[object, ...] = () if cutoff is None else (cutoff,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, sample_question, occurrence_count, first_seen_at,
                       last_seen_at, last_conversation_id, status, note,
                       updated_at
                FROM knowledge_gaps
                {where}
                ORDER BY last_seen_at DESC, id DESC
                """,
                parameters,
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "sample_question": str(row["sample_question"]),
                "occurrence_count": int(row["occurrence_count"]),
                "first_seen_at": int(row["first_seen_at"]),
                "last_seen_at": int(row["last_seen_at"]),
                "last_conversation_id": int(row["last_conversation_id"]),
                "status": str(row["status"]),
                "note": str(row["note"]),
                "updated_at": int(row["updated_at"]),
            }
            for row in rows
        ]

    def _final_events(
        self,
        connection: sqlite3.Connection,
        cutoff: int | None,
    ) -> list[sqlite3.Row]:
        cutoff_clause = "" if cutoff is None else "AND created_at >= ?"
        parameters: tuple[object, ...] = () if cutoff is None else (cutoff,)
        return connection.execute(
            f"""
            WITH ranked AS (
                SELECT id, message_id, conversation_id, outcome, reason,
                       duration_ms, created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY message_id ORDER BY id DESC
                       ) AS event_rank
                FROM service_events
                WHERE outcome <> 'duplicate'
            )
            SELECT id, message_id, conversation_id, outcome, reason,
                   duration_ms, created_at
            FROM ranked
            WHERE event_rank = 1 {cutoff_clause}
            ORDER BY created_at ASC, id ASC
            """,
            parameters,
        ).fetchall()

    def _duplicate_count(
        self,
        connection: sqlite3.Connection,
        cutoff: int | None,
    ) -> int:
        cutoff_clause = "" if cutoff is None else "AND created_at >= ?"
        parameters: tuple[object, ...] = () if cutoff is None else (cutoff,)
        return int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM service_events
                WHERE outcome = 'duplicate' {cutoff_clause}
                """,
                parameters,
            ).fetchone()[0]
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS service_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    conversation_id INTEGER NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN (
                        'replied', 'handed_off', 'ignored_human_mode',
                        'duplicate', 'failed'
                    )),
                    reason TEXT,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_service_events_message
                    ON service_events(message_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_service_events_created
                    ON service_events(created_at);
                CREATE TABLE IF NOT EXISTS knowledge_gaps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    sample_question TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1
                        CHECK(occurrence_count > 0),
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    last_conversation_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'resolved', 'ignored')),
                    note TEXT NOT NULL DEFAULT '' CHECK(length(note) <= 1000),
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_sort
                    ON knowledge_gaps(occurrence_count DESC, last_seen_at DESC);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _validate_event(outcome: object, reason: object) -> None:
    if outcome not in _OUTCOMES:
        raise ValueError("Invalid operations outcome.")
    if outcome == "handed_off":
        valid = reason in _HANDOFF_REASONS
    elif outcome == "failed":
        valid = reason in _FAILURE_REASONS
    else:
        valid = reason is None
    if not valid:
        raise ValueError("Invalid operations reason for outcome.")


def _require_positive_id(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")


def _timestamp(value: int | None) -> int:
    timestamp = int(time.time()) if value is None else value
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ValueError("Timestamp must be a non-negative integer.")
    return timestamp


def _window_cutoff(window: object, now: int | None) -> int | None:
    if window not in {"7d", "30d", "all"}:
        raise ValueError("Invalid operations window.")
    current = _timestamp(now)
    if window == "all":
        return None
    days = 7 if window == "7d" else 30
    return current - days * 86_400


def _rate(numerator: int, denominator: int) -> dict[str, int | float]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else 0,
    }


def _daily_trend(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    days: dict[str, dict[str, object]] = {}
    for row in rows:
        day = datetime.fromtimestamp(
            int(row["created_at"]),
            tz=timezone.utc,
        ).date().isoformat()
        point = days.setdefault(
            day,
            {
                "date": day,
                "customer_messages": 0,
                "replied": 0,
                "handed_off": 0,
                "knowledge_gaps": 0,
            },
        )
        point["customer_messages"] = int(point["customer_messages"]) + 1
        outcome = str(row["outcome"])
        if outcome in {"replied", "handed_off"}:
            point[outcome] = int(point[outcome]) + 1
        if row["reason"] == "knowledge_gap":
            point["knowledge_gaps"] = int(point["knowledge_gaps"]) + 1
    return [days[day] for day in sorted(days)]


def _row_to_gap(row: sqlite3.Row) -> KnowledgeGap:
    return KnowledgeGap(
        id=int(row["id"]),
        fingerprint=str(row["fingerprint"]),
        sample_question=str(row["sample_question"]),
        occurrence_count=int(row["occurrence_count"]),
        first_seen_at=int(row["first_seen_at"]),
        last_seen_at=int(row["last_seen_at"]),
        last_conversation_id=int(row["last_conversation_id"]),
        status=cast(GapStatus, row["status"]),
        note=str(row["note"]),
        updated_at=int(row["updated_at"]),
    )
