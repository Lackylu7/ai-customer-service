import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.chatwoot_events import IncomingMessage
from app.handoff_schema import ensure_handoff_schema
from app.operations_models import Reason


ConversationMode = Literal["ai", "human"]


@dataclass(frozen=True)
class ConversationState:
    conversation_id: int
    dify_conversation_id: str | None
    mode: ConversationMode
    handoff_reason: str | None = None
    human_since: int | None = None
    last_customer_message_id: int | None = None
    last_customer_message_at: int | None = None
    last_human_reply_message_id: int | None = None
    last_human_reply_at: int | None = None
    last_fallback_at: int | None = None
    ai_resume_cutoff_message_id: int | None = None


class SQLiteConversationStore:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def claim_message(self, message_id: int) -> bool:
        _require_positive_id(message_id, "Message ID")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO processed_messages (message_id, status)
                    VALUES (?, 'processing')
                    """,
                    (message_id,),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def complete_message(self, message_id: int) -> None:
        _require_positive_id(message_id, "Message ID")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE processed_messages
                SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                WHERE message_id = ? AND status = 'processing'
                """,
                (message_id,),
            )

    def release_message(self, message_id: int) -> None:
        _require_positive_id(message_id, "Message ID")
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM processed_messages
                WHERE message_id = ? AND status = 'processing'
                """,
                (message_id,),
            )

    def get_conversation(self, conversation_id: int) -> ConversationState | None:
        _require_positive_id(conversation_id, "Conversation ID")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT conversation_id, dify_conversation_id, mode,
                       handoff_reason, human_since,
                       last_customer_message_id, last_customer_message_at,
                       last_human_reply_message_id, last_human_reply_at,
                       last_fallback_at, ai_resume_cutoff_message_id
                FROM conversation_states
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return ConversationState(
            conversation_id=int(row["conversation_id"]),
            dify_conversation_id=row["dify_conversation_id"],
            mode=row["mode"],
            handoff_reason=row["handoff_reason"],
            human_since=_optional_int(row["human_since"]),
            last_customer_message_id=_optional_int(
                row["last_customer_message_id"]
            ),
            last_customer_message_at=_optional_int(row["last_customer_message_at"]),
            last_human_reply_message_id=_optional_int(
                row["last_human_reply_message_id"]
            ),
            last_human_reply_at=_optional_int(row["last_human_reply_at"]),
            last_fallback_at=_optional_int(row["last_fallback_at"]),
            ai_resume_cutoff_message_id=_optional_int(
                row["ai_resume_cutoff_message_id"]
            ),
        )

    def save_conversation(
        self,
        conversation_id: int,
        *,
        dify_conversation_id: str | None,
        mode: ConversationMode,
    ) -> None:
        _require_positive_id(conversation_id, "Conversation ID")
        if mode not in {"ai", "human"}:
            raise ValueError("Conversation mode must be 'ai' or 'human'.")
        normalized_dify_id = (
            dify_conversation_id.strip()
            if isinstance(dify_conversation_id, str) and dify_conversation_id.strip()
            else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_states (
                    conversation_id, dify_conversation_id, mode
                ) VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    dify_conversation_id = excluded.dify_conversation_id,
                    mode = excluded.mode,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (conversation_id, normalized_dify_id, mode),
            )

    def save_handoff(
        self,
        message: IncomingMessage,
        reason: Reason,
        *,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("Timestamp must be a non-negative integer.")
        message_created_at = message.created_at if message.created_at > 0 else timestamp
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_states (
                    conversation_id,
                    dify_conversation_id,
                    mode,
                    handoff_reason,
                    human_since,
                    last_customer_message_id,
                    last_customer_message_at
                ) VALUES (?, NULL, 'human', ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    dify_conversation_id = NULL,
                    mode = 'human',
                    handoff_reason = excluded.handoff_reason,
                    human_since = excluded.human_since,
                    last_customer_message_id = excluded.last_customer_message_id,
                    last_customer_message_at = excluded.last_customer_message_at,
                    last_human_reply_message_id = NULL,
                    last_human_reply_at = NULL,
                    last_fallback_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    message.conversation_id,
                    reason,
                    timestamp,
                    message.message_id,
                    message_created_at,
                ),
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL
                        CHECK(status IN ('processing', 'completed')),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            ensure_handoff_schema(connection)

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


def _require_positive_id(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
