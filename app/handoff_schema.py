import sqlite3


_CONVERSATION_COLUMNS = {
    "handoff_reason": "TEXT",
    "human_since": "INTEGER",
    "last_customer_message_id": "INTEGER",
    "last_customer_message_at": "INTEGER",
    "last_human_reply_message_id": "INTEGER",
    "last_human_reply_at": "INTEGER",
    "last_fallback_at": "INTEGER",
    "ai_resume_cutoff_message_id": "INTEGER",
}


def ensure_handoff_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_states (
            conversation_id INTEGER PRIMARY KEY,
            dify_conversation_id TEXT,
            mode TEXT NOT NULL CHECK(mode IN ('ai', 'human')),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    present = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(conversation_states)")
    }
    for name, declaration in _CONVERSATION_COLUMNS.items():
        if name not in present:
            connection.execute(
                f"ALTER TABLE conversation_states ADD COLUMN {name} {declaration}"
            )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_control_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK(action IN (
                'human_wait_notice', 'human_timeout_reply', 'ai_resumed'
            )),
            result TEXT NOT NULL CHECK(result IN ('succeeded', 'failed')),
            error_code TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_control_events_conversation
        ON conversation_control_events(conversation_id, created_at DESC, id DESC)
        """
    )
