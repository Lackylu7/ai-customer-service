import tempfile
import unittest
from pathlib import Path

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
