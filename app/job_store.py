import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from app.chatwoot_events import HumanReply, IncomingMessage
from app.job_models import (
    ClaimedFromStage,
    Decision,
    DurableJob,
    EnqueueResult,
    JobKind,
    JobStage,
    PreparedAction,
    QueueMetrics,
    ResumeStage,
)
from app.handoff_schema import ensure_handoff_schema
from app.operations_models import Outcome, Reason


_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RECONCILIATION_CURSOR_NAME = "chatwoot_messages"
ResumeStoreStatus = Literal["resumed", "already_ai", "not_found", "conflict"]
ResumePrecheckStatus = ResumeStoreStatus | Literal["ready"]


class SQLiteJobStore:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def enqueue(
        self,
        message: IncomingMessage,
        *,
        now: int | None = None,
    ) -> EnqueueResult:
        created_at = int(time.time()) if now is None else now
        message_created_at = (
            message.created_at if message.created_at > 0 else created_at
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            completed = connection.execute(
                """
                SELECT 1
                FROM processed_messages
                WHERE message_id = ? AND status = 'completed'
                """,
                (message.message_id,),
            ).fetchone()
            if completed is not None:
                return EnqueueResult(status="duplicate", job_id=None)

            state = connection.execute(
                """
                SELECT mode, ai_resume_cutoff_message_id,
                       last_customer_message_at, last_customer_message_id
                FROM conversation_states
                WHERE conversation_id = ?
                """,
                (message.conversation_id,),
            ).fetchone()
            if (
                state is not None
                and state["ai_resume_cutoff_message_id"] is not None
                and message.message_id
                <= int(state["ai_resume_cutoff_message_id"])
            ):
                connection.execute(
                    """
                    INSERT INTO processed_messages (
                        message_id, status, updated_at
                    ) VALUES (?, 'completed', CURRENT_TIMESTAMP)
                    ON CONFLICT(message_id) DO UPDATE SET
                        status = 'completed',
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (message.message_id,),
                )
                return EnqueueResult(status="duplicate", job_id=None)

            if state is not None and state["mode"] == "human":
                current_customer = (
                    None
                    if state["last_customer_message_at"] is None
                    or state["last_customer_message_id"] is None
                    else (
                        int(state["last_customer_message_at"]),
                        int(state["last_customer_message_id"]),
                    )
                )
                incoming_customer = (message_created_at, message.message_id)
                if (
                    current_customer is None
                    or incoming_customer > current_customer
                ):
                    connection.execute(
                        """
                        UPDATE conversation_states
                        SET last_customer_message_at = ?,
                            last_customer_message_id = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE conversation_id = ? AND mode = 'human'
                        """,
                        (
                            message_created_at,
                            message.message_id,
                            message.conversation_id,
                        ),
                    )

            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_jobs (
                    message_id,
                    account_id,
                    conversation_id,
                    sender_id,
                    content,
                    labels_json,
                    stage,
                    job_kind,
                    outgoing_source_id,
                    message_created_at,
                    available_at,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 'standard', ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.account_id,
                    message.conversation_id,
                    message.sender_id,
                    message.content,
                    json.dumps(message.labels, ensure_ascii=False),
                    f"ai-cs:{message.message_id}:reply",
                    message_created_at,
                    created_at,
                    created_at,
                    created_at,
                ),
            )
            if cursor.rowcount == 0:
                return EnqueueResult(status="duplicate", job_id=None)
            return EnqueueResult(status="queued", job_id=int(cursor.lastrowid))

    def claim_next(
        self,
        worker_id: str,
        *,
        now: int,
        lease_seconds: int,
    ) -> DurableJob | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("Worker claim configuration is invalid.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                """
                SELECT j.id, j.stage, j.resume_stage, j.job_kind
                FROM inbound_jobs AS j
                WHERE j.stage IN (
                    'queued', 'human_wait', 'ready_to_deliver', 'retry_wait'
                )
                  AND j.available_at <= ?
                  AND (j.stage != 'retry_wait' OR j.resume_stage IS NOT NULL)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM inbound_jobs AS earlier
                      WHERE earlier.conversation_id = j.conversation_id
                        AND earlier.id < j.id
                        AND earlier.stage NOT IN ('completed')
                  )
                ORDER BY j.id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if candidate is None:
                return None

            source_stage = candidate["stage"]
            if source_stage in {"queued", "human_wait"}:
                claimed_stage: ResumeStage = "generating"
            elif source_stage == "ready_to_deliver":
                claimed_stage = "delivering"
            else:
                claimed_stage = cast(ResumeStage, candidate["resume_stage"])

            connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = ?,
                    generation_attempts = generation_attempts + CASE
                        WHEN ? = 'generating' AND job_kind = 'standard' THEN 1
                        ELSE 0
                    END,
                    delivery_attempts = delivery_attempts + CASE WHEN ? = 'delivering' THEN 1 ELSE 0 END,
                    locked_at = ?,
                    locked_by = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    claimed_stage,
                    claimed_stage,
                    claimed_stage,
                    now,
                    worker_id.strip(),
                    now,
                    candidate["id"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM inbound_jobs WHERE id = ?",
                (candidate["id"],),
            ).fetchone()
        return self._row_to_job(
            row,
            claimed_from_stage=cast(ClaimedFromStage, source_stage),
        )

    def defer_human_wait(
        self,
        job_id: int,
        *,
        available_at: int,
        now: int,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT j.message_id, j.message_created_at, c.mode,
                       c.last_human_reply_at, c.last_human_reply_message_id
                FROM inbound_jobs AS j
                LEFT JOIN conversation_states AS c
                  ON c.conversation_id = j.conversation_id
                WHERE j.id = ? AND j.stage = 'generating'
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Job {job_id} cannot make this transition.")
            replied = _event_not_before(
                row["last_human_reply_at"],
                row["last_human_reply_message_id"],
                int(row["message_created_at"]),
                int(row["message_id"]),
            )
            if row["mode"] != "human" or replied:
                self._complete_row(connection, job_id, now=now)
                return False
            cursor = connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = 'human_wait',
                    job_kind = 'human_pending',
                    outgoing_source_id =
                        'ai-cs:' || message_id || ':human-fallback',
                    available_at = ?,
                    locked_at = NULL,
                    locked_by = NULL,
                    updated_at = ?
                WHERE id = ? AND stage = 'generating'
                """,
                (available_at, now, job_id),
            )
            self._require_transition(cursor, job_id)
            return True

    def record_human_reply(
        self,
        reply: HumanReply,
        *,
        now: int | None = None,
    ) -> int:
        timestamp = int(time.time()) if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                """
                SELECT mode, last_human_reply_at, last_human_reply_message_id
                FROM conversation_states
                WHERE conversation_id = ?
                """,
                (reply.conversation_id,),
            ).fetchone()
            if state is None or state["mode"] != "human":
                return 0
            current = (
                None
                if state["last_human_reply_at"] is None
                or state["last_human_reply_message_id"] is None
                else (
                    int(state["last_human_reply_at"]),
                    int(state["last_human_reply_message_id"]),
                )
            )
            incoming = (reply.created_at, reply.message_id)
            if current is None or incoming > current:
                connection.execute(
                    """
                    UPDATE conversation_states
                    SET last_human_reply_at = ?,
                        last_human_reply_message_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE conversation_id = ? AND mode = 'human'
                    """,
                    (
                        reply.created_at,
                        reply.message_id,
                        reply.conversation_id,
                    ),
                )
            rows = connection.execute(
                """
                SELECT id, message_id, message_created_at
                FROM inbound_jobs
                WHERE conversation_id = ?
                  AND job_kind != 'standard'
                  AND stage IN (
                      'human_wait', 'generating',
                      'ready_to_deliver', 'retry_wait'
                  )
                """,
                (reply.conversation_id,),
            ).fetchall()
            cancelled = 0
            for row in rows:
                if (
                    int(row["message_created_at"]),
                    int(row["message_id"]),
                ) <= incoming:
                    self._complete_row(connection, int(row["id"]), now=timestamp)
                    cancelled += 1
            return cancelled

    def confirm_human_fallback(
        self,
        job_id: int,
        *,
        cooldown_seconds: int,
        now: int,
    ) -> bool:
        if cooldown_seconds <= 0:
            raise ValueError("Fallback cooldown must be positive.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._fallback_state(connection, job_id, stage="generating")
            eligible = (
                row is not None
                and row["mode"] == "human"
                and not _event_not_before(
                    row["last_human_reply_at"],
                    row["last_human_reply_message_id"],
                    int(row["message_created_at"]),
                    int(row["message_id"]),
                )
                and (
                    row["last_fallback_at"] is None
                    or int(row["last_fallback_at"]) <= now - cooldown_seconds
                )
            )
            if row is None:
                raise ValueError(f"Job {job_id} cannot make this transition.")
            if not eligible:
                self._complete_row(connection, job_id, now=now)
            return eligible

    def save_human_fallback_prepared(
        self,
        job_id: int,
        action: PreparedAction,
        *,
        job_kind: Literal["human_timeout_reply", "human_wait_notice"],
        processing_duration_ms: int,
        now: int,
    ) -> bool:
        if job_kind not in {"human_timeout_reply", "human_wait_notice"}:
            raise ValueError("Human fallback job kind is invalid.")
        if processing_duration_ms < 0:
            raise ValueError("Processing duration cannot be negative.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._fallback_state(connection, job_id, stage="generating")
            if row is None:
                raise ValueError(f"Job {job_id} cannot make this transition.")
            if row["mode"] != "human" or _event_not_before(
                row["last_human_reply_at"],
                row["last_human_reply_message_id"],
                int(row["message_created_at"]),
                int(row["message_id"]),
            ):
                self._complete_row(connection, job_id, now=now)
                return False
            cursor = connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = 'ready_to_deliver',
                    resume_stage = NULL,
                    job_kind = ?,
                    decision = ?,
                    outcome = ?,
                    reply_text = ?,
                    dify_conversation_id = ?,
                    reason = ?,
                    record_knowledge_gap = ?,
                    processing_duration_ms = ?,
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error_code = NULL,
                    available_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND stage = 'generating'
                  AND job_kind = 'human_pending'
                """,
                (
                    job_kind,
                    action.decision,
                    action.outcome,
                    action.customer_text,
                    action.dify_conversation_id,
                    action.reason,
                    int(action.record_knowledge_gap),
                    processing_duration_ms,
                    now,
                    now,
                    job_id,
                ),
            )
            self._require_transition(cursor, job_id)
            return True

    def can_deliver_human_fallback(
        self,
        job_id: int,
        *,
        now: int,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._fallback_state(connection, job_id, stage="delivering")
            if row is None:
                raise ValueError(f"Job {job_id} cannot make this transition.")
            eligible = row["mode"] == "human" and not _event_not_before(
                row["last_human_reply_at"],
                row["last_human_reply_message_id"],
                int(row["message_created_at"]),
                int(row["message_id"]),
            )
            if not eligible:
                self._complete_row(connection, job_id, now=now)
            return eligible

    def complete_human_fallback(self, job_id: int, *, now: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT conversation_id, job_kind
                FROM inbound_jobs
                WHERE id = ?
                  AND stage = 'delivering'
                  AND job_kind IN (
                      'human_timeout_reply', 'human_wait_notice'
                  )
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Job {job_id} cannot make this transition.")
            connection.execute(
                """
                UPDATE conversation_states
                SET last_fallback_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE conversation_id = ? AND mode = 'human'
                """,
                (now, row["conversation_id"]),
            )
            connection.execute(
                """
                INSERT INTO conversation_control_events (
                    conversation_id, action, result, error_code, created_at
                ) VALUES (?, ?, 'succeeded', NULL, ?)
                """,
                (row["conversation_id"], row["job_kind"], now),
            )
            self._complete_row(connection, job_id, now=now)

    def complete_without_delivery(self, job_id: int, *, now: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._complete_row(connection, job_id, now=now)

    def get_resume_precheck(self, conversation_id: int) -> ResumePrecheckStatus:
        self._validate_positive_id(conversation_id, "Conversation ID")
        with self._connect() as connection:
            state = connection.execute(
                """
                SELECT mode FROM conversation_states WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if state is None:
                return "not_found"
            if state["mode"] == "ai":
                return "already_ai"
            blocking = connection.execute(
                """
                SELECT 1 FROM inbound_jobs
                WHERE conversation_id = ?
                  AND stage IN (
                      'generating', 'ready_to_deliver', 'delivering',
                      'retry_wait', 'failed'
                  )
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            return "conflict" if blocking is not None else "ready"

    def resume_ai_transaction(
        self,
        conversation_id: int,
        *,
        cutoff_message_id: int,
        now: int,
    ) -> ResumeStoreStatus:
        self._validate_positive_id(conversation_id, "Conversation ID")
        self._validate_positive_id(cutoff_message_id, "Cutoff message ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                """
                SELECT mode FROM conversation_states WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if state is None:
                return "not_found"
            if state["mode"] == "ai":
                return "already_ai"
            blocking = connection.execute(
                """
                SELECT 1 FROM inbound_jobs
                WHERE conversation_id = ?
                  AND stage IN (
                      'generating', 'ready_to_deliver', 'delivering',
                      'retry_wait', 'failed'
                  )
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if blocking is not None:
                return "conflict"

            self._complete_safe_jobs(
                connection,
                conversation_id,
                cutoff_message_id=cutoff_message_id,
                now=now,
            )
            cursor = connection.execute(
                """
                UPDATE conversation_states
                SET mode = 'ai',
                    dify_conversation_id = NULL,
                    handoff_reason = NULL,
                    human_since = NULL,
                    last_customer_message_id = NULL,
                    last_customer_message_at = NULL,
                    last_human_reply_message_id = NULL,
                    last_human_reply_at = NULL,
                    last_fallback_at = NULL,
                    ai_resume_cutoff_message_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE conversation_id = ? AND mode = 'human'
                """,
                (cutoff_message_id, conversation_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return "conflict"
            connection.execute(
                """
                INSERT INTO conversation_control_events (
                    conversation_id, action, result, error_code, created_at
                ) VALUES (?, 'ai_resumed', 'succeeded', NULL, ?)
                """,
                (conversation_id, now),
            )
            return "resumed"

    def record_control_failure(
        self,
        conversation_id: int,
        action: Literal["ai_resumed"],
        error_code: str,
        *,
        now: int,
    ) -> None:
        self._validate_positive_id(conversation_id, "Conversation ID")
        if action != "ai_resumed":
            raise ValueError("Control action is invalid.")
        self._validate_error_code(error_code)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_control_events (
                    conversation_id, action, result, error_code, created_at
                ) VALUES (?, ?, 'failed', ?, ?)
                """,
                (conversation_id, action, error_code, now),
            )

    def save_prepared(
        self,
        job_id: int,
        action: PreparedAction,
        *,
        processing_duration_ms: int,
        now: int,
    ) -> None:
        if processing_duration_ms < 0:
            raise ValueError("Processing duration cannot be negative.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = 'ready_to_deliver',
                    resume_stage = NULL,
                    decision = ?,
                    outcome = ?,
                    reply_text = ?,
                    dify_conversation_id = ?,
                    reason = ?,
                    record_knowledge_gap = ?,
                    processing_duration_ms = ?,
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error_code = NULL,
                    available_at = ?,
                    updated_at = ?
                WHERE id = ? AND stage = 'generating'
                """,
                (
                    action.decision,
                    action.outcome,
                    action.customer_text,
                    action.dify_conversation_id,
                    action.reason,
                    int(action.record_knowledge_gap),
                    processing_duration_ms,
                    now,
                    now,
                    job_id,
                ),
            )
            self._require_transition(cursor, job_id)

    def schedule_retry(
        self,
        job_id: int,
        *,
        resume_stage: ResumeStage,
        error_code: str,
        available_at: int,
        now: int,
    ) -> None:
        self._validate_error_code(error_code)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = 'retry_wait',
                    resume_stage = ?,
                    available_at = ?,
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error_code = ?,
                    updated_at = ?
                WHERE id = ? AND stage IN ('generating', 'delivering')
                """,
                (resume_stage, available_at, error_code, now, job_id),
            )
            self._require_transition(cursor, job_id)

    def complete(self, job_id: int, *, now: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT message_id FROM inbound_jobs WHERE id = ? AND stage = 'delivering'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Job {job_id} cannot make this transition.")
            connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = 'completed',
                    resume_stage = NULL,
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error_code = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
            connection.execute(
                """
                INSERT INTO processed_messages (message_id, status, updated_at)
                VALUES (?, 'completed', CURRENT_TIMESTAMP)
                ON CONFLICT(message_id) DO UPDATE SET
                    status = 'completed',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (row["message_id"],),
            )

    def fail(
        self,
        job_id: int,
        *,
        resume_stage: ResumeStage,
        error_code: str,
        now: int,
    ) -> None:
        self._validate_error_code(error_code)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT conversation_id, job_kind
                FROM inbound_jobs
                WHERE id = ? AND stage IN ('generating', 'delivering')
                """,
                (job_id,),
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = 'failed',
                    resume_stage = ?,
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error_code = ?,
                    updated_at = ?
                WHERE id = ? AND stage IN ('generating', 'delivering')
                """,
                (resume_stage, error_code, now, job_id),
            )
            self._require_transition(cursor, job_id)
            if row is not None and row["job_kind"] in {
                "human_timeout_reply",
                "human_wait_notice",
            }:
                connection.execute(
                    """
                    INSERT INTO conversation_control_events (
                        conversation_id, action, result, error_code, created_at
                    ) VALUES (?, ?, 'failed', ?, ?)
                    """,
                    (
                        row["conversation_id"],
                        row["job_kind"],
                        error_code,
                        now,
                    ),
                )

    def recover_expired_leases(self, *, now: int, lease_seconds: int) -> int:
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = 'retry_wait',
                    resume_stage = CASE
                        WHEN stage = 'generating' THEN 'generating'
                        ELSE 'delivering'
                    END,
                    available_at = ?,
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error_code = 'lease_expired',
                    updated_at = ?
                WHERE stage IN ('generating', 'delivering')
                  AND locked_at IS NOT NULL
                  AND locked_at <= ?
                """,
                (now, now, now - lease_seconds),
            )
            return cursor.rowcount

    def record_heartbeat(self, worker_id: str, *, now: int) -> None:
        normalized_worker_id = worker_id.strip()
        if not normalized_worker_id:
            raise ValueError("Worker ID is required.")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_heartbeats (worker_id, heartbeat_at)
                VALUES (?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at
                """,
                (normalized_worker_id, now),
            )

    def metrics(self, *, now: int) -> QueueMetrics:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE
                        WHEN stage IN ('queued', 'human_wait') THEN 1
                        ELSE 0
                    END), 0) AS queued,
                    COALESCE(SUM(CASE WHEN stage IN ('generating', 'delivering') THEN 1 ELSE 0 END), 0) AS processing,
                    COALESCE(SUM(CASE WHEN stage = 'retry_wait' THEN 1 ELSE 0 END), 0) AS retry_wait,
                    COALESCE(SUM(CASE WHEN stage = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
                    MIN(CASE
                        WHEN stage IN ('queued', 'human_wait', 'retry_wait')
                        THEN created_at
                    END) AS oldest_wait_at
                FROM inbound_jobs
                """
            ).fetchone()
            heartbeat = connection.execute(
                "SELECT MAX(heartbeat_at) AS heartbeat_at FROM worker_heartbeats"
            ).fetchone()["heartbeat_at"]
        oldest_wait_at = row["oldest_wait_at"]
        oldest_wait_seconds = 0 if oldest_wait_at is None else max(0, now - oldest_wait_at)
        return QueueMetrics(
            queued=int(row["queued"]),
            processing=int(row["processing"]),
            retry_wait=int(row["retry_wait"]),
            failed=int(row["failed"]),
            oldest_wait_seconds=int(oldest_wait_seconds),
            last_worker_heartbeat=None if heartbeat is None else int(heartbeat),
        )

    def is_writable(self) -> bool:
        connection = sqlite3.connect(self._database_path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE queue_meta
                SET value = value
                WHERE name = 'readiness_probe'
                """
            )
            connection.rollback()
            return cursor.rowcount == 1
        except sqlite3.Error:
            connection.rollback()
            return False
        finally:
            connection.close()

    def get_reconciliation_cursor(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cursor_at FROM reconciliation_state WHERE name = ?",
                (_RECONCILIATION_CURSOR_NAME,),
            ).fetchone()
        return None if row is None else int(row["cursor_at"])

    def initialize_reconciliation_cursor(self, *, now: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO reconciliation_state (name, cursor_at)
                VALUES (?, ?)
                """,
                (_RECONCILIATION_CURSOR_NAME, now),
            )
            return cursor.rowcount == 1

    def advance_reconciliation_cursor(self, *, expected: int, new_value: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reconciliation_state
                SET cursor_at = ?
                WHERE name = ? AND cursor_at = ?
                """,
                (new_value, _RECONCILIATION_CURSOR_NAME, expected),
            )
            return cursor.rowcount == 1

    def record_dependency_status(
        self,
        name: Literal["dify", "chatwoot"],
        *,
        ok: bool,
        error_code: str | None,
        checked_at: int,
    ) -> None:
        if name not in {"dify", "chatwoot"}:
            raise ValueError("Dependency name is invalid.")
        if error_code is not None:
            self._validate_error_code(error_code)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dependency_status (name, ok, error_code, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    ok = excluded.ok,
                    error_code = excluded.error_code,
                    checked_at = excluded.checked_at
                """,
                (name, int(ok), error_code, checked_at),
            )

    def dependency_statuses(self) -> dict[str, dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, ok, error_code, checked_at FROM dependency_status ORDER BY name"
            ).fetchall()
        return {
            str(row["name"]): {
                "ok": bool(row["ok"]),
                "error_code": row["error_code"],
                "checked_at": int(row["checked_at"]),
            }
            for row in rows
        }

    def retry_failed_message(self, message_id: int, *, now: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE inbound_jobs
                SET stage = 'retry_wait',
                    available_at = ?,
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error_code = NULL,
                    updated_at = ?
                WHERE message_id = ?
                  AND stage = 'failed'
                  AND resume_stage IS NOT NULL
                """,
                (now, now, message_id),
            )
            return cursor.rowcount == 1

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS inbound_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL UNIQUE,
                    account_id INTEGER NOT NULL,
                    conversation_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    labels_json TEXT NOT NULL DEFAULT '[]',
                    stage TEXT NOT NULL CHECK(stage IN (
                        'queued','generating','human_wait',
                        'ready_to_deliver','delivering',
                        'retry_wait','completed','failed'
                    )),
                    resume_stage TEXT CHECK(resume_stage IN ('generating','delivering')),
                    job_kind TEXT NOT NULL DEFAULT 'standard'
                        CHECK(job_kind IN (
                            'standard','human_pending',
                            'human_timeout_reply','human_wait_notice'
                        )),
                    decision TEXT CHECK(decision IN ('reply','handoff','ignore')),
                    outcome TEXT,
                    reply_text TEXT,
                    dify_conversation_id TEXT,
                    reason TEXT,
                    record_knowledge_gap INTEGER NOT NULL DEFAULT 0
                        CHECK(record_knowledge_gap IN (0,1)),
                    outgoing_source_id TEXT NOT NULL,
                    generation_attempts INTEGER NOT NULL DEFAULT 0,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    processing_duration_ms INTEGER NOT NULL DEFAULT 0,
                    available_at INTEGER NOT NULL,
                    locked_at INTEGER,
                    locked_by TEXT,
                    last_error_code TEXT,
                    message_created_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbound_jobs_ready
                    ON inbound_jobs(stage, available_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbound_jobs_conversation
                    ON inbound_jobs(conversation_id, id, stage);
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('processing', 'completed')),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS worker_heartbeats (
                    worker_id TEXT PRIMARY KEY,
                    heartbeat_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_state (
                    name TEXT PRIMARY KEY,
                    cursor_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dependency_status (
                    name TEXT PRIMARY KEY CHECK(name IN ('dify','chatwoot')),
                    ok INTEGER NOT NULL CHECK(ok IN (0,1)),
                    error_code TEXT,
                    checked_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queue_meta (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO queue_meta (name, value)
                VALUES ('readiness_probe', 0);
                """
            )
            ensure_handoff_schema(connection)
            self._migrate_inbound_jobs_for_handoff(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbound_jobs_ready
                ON inbound_jobs(stage, available_at, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbound_jobs_conversation
                ON inbound_jobs(conversation_id, id, stage)
                """
            )

    def _migrate_inbound_jobs_for_handoff(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        table_sql_row = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'inbound_jobs'
            """
        ).fetchone()
        if table_sql_row is None:
            return
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(inbound_jobs)")
        }
        table_sql = str(table_sql_row[0] or "")
        if (
            "'human_wait'" in table_sql
            and "message_created_at" in columns
            and "job_kind" in columns
        ):
            return

        if connection.in_transaction:
            connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP INDEX IF EXISTS idx_inbound_jobs_ready")
        connection.execute("DROP INDEX IF EXISTS idx_inbound_jobs_conversation")
        connection.execute("ALTER TABLE inbound_jobs RENAME TO inbound_jobs_v1")
        connection.execute(
            """
            CREATE TABLE inbound_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL UNIQUE,
                account_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                labels_json TEXT NOT NULL DEFAULT '[]',
                stage TEXT NOT NULL CHECK(stage IN (
                    'queued','generating','human_wait',
                    'ready_to_deliver','delivering',
                    'retry_wait','completed','failed'
                )),
                resume_stage TEXT CHECK(resume_stage IN (
                    'generating','delivering'
                )),
                job_kind TEXT NOT NULL DEFAULT 'standard'
                    CHECK(job_kind IN (
                        'standard','human_pending',
                        'human_timeout_reply','human_wait_notice'
                    )),
                decision TEXT CHECK(decision IN ('reply','handoff','ignore')),
                outcome TEXT,
                reply_text TEXT,
                dify_conversation_id TEXT,
                reason TEXT,
                record_knowledge_gap INTEGER NOT NULL DEFAULT 0
                    CHECK(record_knowledge_gap IN (0,1)),
                outgoing_source_id TEXT NOT NULL,
                generation_attempts INTEGER NOT NULL DEFAULT 0,
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                processing_duration_ms INTEGER NOT NULL DEFAULT 0,
                available_at INTEGER NOT NULL,
                locked_at INTEGER,
                locked_by TEXT,
                last_error_code TEXT,
                message_created_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO inbound_jobs (
                id, message_id, account_id, conversation_id, sender_id,
                content, labels_json, stage, resume_stage, job_kind,
                decision, outcome, reply_text, dify_conversation_id, reason,
                record_knowledge_gap, outgoing_source_id,
                generation_attempts, delivery_attempts,
                processing_duration_ms, available_at, locked_at, locked_by,
                last_error_code, message_created_at, created_at, updated_at
            )
            SELECT id, message_id, account_id, conversation_id, sender_id,
                   content, labels_json, stage, resume_stage, 'standard',
                   decision, outcome, reply_text, dify_conversation_id, reason,
                   record_knowledge_gap, outgoing_source_id,
                   generation_attempts, delivery_attempts,
                   processing_duration_ms, available_at, locked_at, locked_by,
                   last_error_code, created_at, created_at, updated_at
            FROM inbound_jobs_v1
            """
        )
        connection.execute("DROP TABLE inbound_jobs_v1")
        connection.execute(
            """
            CREATE INDEX idx_inbound_jobs_ready
            ON inbound_jobs(stage, available_at, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_inbound_jobs_conversation
            ON inbound_jobs(conversation_id, id, stage)
            """
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_error_code(error_code: str) -> None:
        if not isinstance(error_code, str) or _ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("A stable snake-case error code is required.")

    @staticmethod
    def _require_transition(cursor: sqlite3.Cursor, job_id: int) -> None:
        if cursor.rowcount != 1:
            raise ValueError(f"Job {job_id} cannot make this transition.")

    def _fallback_state(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        *,
        stage: Literal["generating", "delivering"],
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT j.message_id, j.message_created_at, j.stage, j.job_kind,
                   c.mode, c.last_human_reply_at,
                   c.last_human_reply_message_id, c.last_fallback_at
            FROM inbound_jobs AS j
            JOIN conversation_states AS c
              ON c.conversation_id = j.conversation_id
            WHERE j.id = ?
              AND j.stage = ?
              AND j.job_kind != 'standard'
            """,
            (job_id, stage),
        ).fetchone()

    def _complete_row(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        *,
        now: int,
    ) -> None:
        row = connection.execute(
            "SELECT message_id FROM inbound_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} was not found.")
        connection.execute(
            """
            UPDATE inbound_jobs
            SET stage = 'completed',
                resume_stage = NULL,
                locked_at = NULL,
                locked_by = NULL,
                last_error_code = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, job_id),
        )
        connection.execute(
            """
            INSERT INTO processed_messages (message_id, status, updated_at)
            VALUES (?, 'completed', CURRENT_TIMESTAMP)
            ON CONFLICT(message_id) DO UPDATE SET
                status = 'completed',
                updated_at = CURRENT_TIMESTAMP
            """,
            (row["message_id"],),
        )

    def _complete_safe_jobs(
        self,
        connection: sqlite3.Connection,
        conversation_id: int,
        *,
        cutoff_message_id: int,
        now: int,
    ) -> None:
        rows = connection.execute(
            """
            SELECT id FROM inbound_jobs
            WHERE conversation_id = ?
              AND message_id <= ?
              AND stage IN ('queued', 'human_wait')
            """,
            (conversation_id, cutoff_message_id),
        ).fetchall()
        for row in rows:
            self._complete_row(connection, int(row["id"]), now=now)

    @staticmethod
    def _validate_positive_id(value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer.")

    @staticmethod
    def _row_to_job(
        row: sqlite3.Row,
        *,
        claimed_from_stage: ClaimedFromStage | None = None,
    ) -> DurableJob:
        raw_labels = json.loads(row["labels_json"])
        labels = tuple(label for label in raw_labels if isinstance(label, str))
        action = SQLiteJobStore._row_to_action(row)
        return DurableJob(
            id=int(row["id"]),
            message_id=int(row["message_id"]),
            account_id=int(row["account_id"]),
            conversation_id=int(row["conversation_id"]),
            sender_id=int(row["sender_id"]),
            content=str(row["content"]),
            labels=labels,
            stage=cast(JobStage, row["stage"]),
            resume_stage=cast(ResumeStage | None, row["resume_stage"]),
            action=action,
            outgoing_source_id=str(row["outgoing_source_id"]),
            generation_attempts=int(row["generation_attempts"]),
            delivery_attempts=int(row["delivery_attempts"]),
            processing_duration_ms=int(row["processing_duration_ms"]),
            available_at=int(row["available_at"]),
            locked_at=None if row["locked_at"] is None else int(row["locked_at"]),
            locked_by=row["locked_by"],
            last_error_code=row["last_error_code"],
            claimed_from_stage=claimed_from_stage,
            message_created_at=int(row["message_created_at"]),
            received_at=int(row["created_at"]),
            job_kind=cast(JobKind, row["job_kind"]),
        )

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> PreparedAction | None:
        if row["decision"] is None:
            return None
        return PreparedAction(
            decision=cast(Decision, row["decision"]),
            outcome=cast(Outcome, row["outcome"]),
            customer_text=row["reply_text"],
            dify_conversation_id=row["dify_conversation_id"],
            reason=cast(Reason | None, row["reason"]),
            record_knowledge_gap=bool(row["record_knowledge_gap"]),
        )


def _event_not_before(
    candidate_at: object,
    candidate_id: object,
    target_at: int,
    target_id: int,
) -> bool:
    if candidate_at is None or candidate_id is None:
        return False
    return (int(candidate_at), int(candidate_id)) >= (target_at, target_id)
