import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ConversationMode = Literal["ai", "human"]


@dataclass(frozen=True)
class ConversationState:
    conversation_id: int
    dify_conversation_id: str | None
    mode: ConversationMode


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
                SELECT conversation_id, dify_conversation_id, mode
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
                CREATE TABLE IF NOT EXISTS conversation_states (
                    conversation_id INTEGER PRIMARY KEY,
                    dify_conversation_id TEXT,
                    mode TEXT NOT NULL CHECK(mode IN ('ai', 'human')),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
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


def _require_positive_id(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
