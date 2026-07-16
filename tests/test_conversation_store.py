import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.chatwoot_events import IncomingMessage
from app.conversation_store import ConversationState, SQLiteConversationStore


class SQLiteConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "nested" / "state.db"
        self.store = SQLiteConversationStore(self.database_path)

    def test_claim_message_is_duplicate_safe_and_releasable(self) -> None:
        self.assertTrue(self.store.claim_message(101))
        self.assertFalse(self.store.claim_message(101))

        self.store.release_message(101)

        self.assertTrue(self.store.claim_message(101))

    def test_completed_message_cannot_be_claimed_or_released(self) -> None:
        self.assertTrue(self.store.claim_message(102))
        self.store.complete_message(102)

        self.store.release_message(102)

        self.assertFalse(self.store.claim_message(102))

    def test_conversation_state_round_trips_and_updates(self) -> None:
        self.assertIsNone(self.store.get_conversation(7))

        self.store.save_conversation(
            7,
            dify_conversation_id="dify-7",
            mode="ai",
        )
        self.assertEqual(
            self.store.get_conversation(7),
            ConversationState(
                conversation_id=7,
                dify_conversation_id="dify-7",
                mode="ai",
            ),
        )

        self.store.save_conversation(
            7,
            dify_conversation_id=None,
            mode="human",
        )
        self.assertEqual(
            self.store.get_conversation(7),
            ConversationState(
                conversation_id=7,
                dify_conversation_id=None,
                mode="human",
            ),
        )

    def test_existing_conversation_schema_is_migrated_without_losing_state(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE conversation_states (
                    conversation_id INTEGER PRIMARY KEY,
                    dify_conversation_id TEXT,
                    mode TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO conversation_states (
                    conversation_id, dify_conversation_id, mode
                ) VALUES (42, 'dify-old', 'human');
                """
            )
            connection.commit()

        migrated = SQLiteConversationStore(legacy_path).get_conversation(42)

        self.assertIsNotNone(migrated)
        assert migrated is not None
        self.assertEqual(migrated.mode, "human")
        self.assertEqual(migrated.dify_conversation_id, "dify-old")
        self.assertIsNone(migrated.last_human_reply_at)
        self.assertIsNone(migrated.ai_resume_cutoff_message_id)

    def test_save_handoff_records_reason_and_trigger_message(self) -> None:
        message = IncomingMessage(
            message_id=101,
            account_id=1,
            conversation_id=42,
            sender_id=9,
            content="我要人工",
            labels=(),
            created_at=1_000,
        )

        self.store.save_handoff(
            message,
            "explicit_human_request",
            now=1_001,
        )

        state = self.store.get_conversation(42)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.mode, "human")
        self.assertEqual(state.handoff_reason, "explicit_human_request")
        self.assertEqual(state.human_since, 1_001)
        self.assertEqual(state.last_customer_message_id, 101)
        self.assertEqual(state.last_customer_message_at, 1_000)

    def test_rejects_invalid_identifiers_and_mode(self) -> None:
        with self.assertRaises(ValueError):
            self.store.claim_message(0)
        with self.assertRaises(ValueError):
            self.store.save_conversation(
                7,
                dify_conversation_id=None,
                mode="automatic",
            )


if __name__ == "__main__":
    unittest.main()
