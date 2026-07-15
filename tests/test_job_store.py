import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.chatwoot_events import IncomingMessage
from app.job_models import PreparedAction
from app.job_store import SQLiteJobStore


def message(*, message_id: int, conversation_id: int) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        account_id=1,
        conversation_id=conversation_id,
        sender_id=99,
        content=f"问题 {message_id}",
        labels=("vip",),
    )


class SQLiteJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "nested" / "state.db"
        self.store = SQLiteJobStore(self.database_path)

    def test_enqueue_is_idempotent_and_claim_preserves_conversation_order(self) -> None:
        first = self.store.enqueue(message(message_id=101, conversation_id=7), now=1_000)
        duplicate = self.store.enqueue(message(message_id=101, conversation_id=7), now=1_001)
        self.assertEqual((first.status, duplicate.status), ("queued", "duplicate"))

        self.store.enqueue(message(message_id=102, conversation_id=7), now=1_002)
        self.store.enqueue(message(message_id=103, conversation_id=8), now=1_003)
        job_one = self.store.claim_next("worker-a", now=1_004, lease_seconds=300)
        job_two = self.store.claim_next("worker-b", now=1_004, lease_seconds=300)
        self.assertIsNotNone(job_one)
        self.assertIsNotNone(job_two)
        assert job_one is not None
        assert job_two is not None
        self.assertEqual(job_one.message_id, 101)
        self.assertEqual(job_two.message_id, 103)
        self.assertEqual(job_one.stage, "generating")
        self.assertEqual(job_one.generation_attempts, 1)
        self.assertEqual(job_one.outgoing_source_id, "ai-cs:101:reply")

        self.store.save_prepared(
            job_one.id,
            PreparedAction.reply("答复", "dify-101"),
            processing_duration_ms=120,
            now=1_005,
        )
        delivery = self.store.claim_next("worker-a", now=1_006, lease_seconds=300)
        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.assertEqual(delivery.stage, "delivering")
        self.assertEqual(delivery.delivery_attempts, 1)
        self.assertEqual(delivery.action, PreparedAction.reply("答复", "dify-101"))
        self.store.complete(delivery.id, now=1_007)
        next_same_conversation = self.store.claim_next(
            "worker-a", now=1_008, lease_seconds=300
        )
        self.assertIsNotNone(next_same_conversation)
        assert next_same_conversation is not None
        self.assertEqual(next_same_conversation.message_id, 102)

    def test_completed_legacy_message_is_duplicate_without_job(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO processed_messages (message_id, status) VALUES (?, 'completed')",
                (201,),
            )
            connection.commit()

        result = self.store.enqueue(message(message_id=201, conversation_id=20), now=2_000)

        self.assertEqual((result.status, result.job_id), ("duplicate", None))
        with closing(sqlite3.connect(self.database_path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM inbound_jobs WHERE message_id = 201"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_expired_generating_lease_recovers_to_retry_wait(self) -> None:
        self.store.enqueue(message(message_id=301, conversation_id=30), now=1_000)
        claimed = self.store.claim_next("worker-a", now=1_004, lease_seconds=300)
        self.assertIsNotNone(claimed)

        self.assertEqual(
            self.store.recover_expired_leases(now=1_303, lease_seconds=300),
            0,
        )
        self.assertEqual(
            self.store.recover_expired_leases(now=1_400, lease_seconds=300),
            1,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT stage, resume_stage, locked_at, locked_by FROM inbound_jobs WHERE message_id = 301"
            ).fetchone()
        self.assertEqual(row, ("retry_wait", "generating", None, None))

    def test_failed_job_blocks_conversation_until_replayed(self) -> None:
        self.store.enqueue(message(message_id=401, conversation_id=40), now=1_000)
        self.store.enqueue(message(message_id=402, conversation_id=40), now=1_001)
        failed = self.store.claim_next("worker-a", now=1_002, lease_seconds=300)
        self.assertIsNotNone(failed)
        assert failed is not None
        self.store.fail(
            failed.id,
            resume_stage="generating",
            error_code="dify_auth",
            now=1_003,
        )

        self.assertIsNone(self.store.claim_next("worker-a", now=1_004, lease_seconds=300))
        self.assertTrue(self.store.retry_failed_message(401, now=1_005))
        replayed = self.store.claim_next("worker-a", now=1_006, lease_seconds=300)
        self.assertIsNotNone(replayed)
        assert replayed is not None
        self.assertEqual((replayed.message_id, replayed.stage), (401, "generating"))
        self.assertFalse(self.store.retry_failed_message(401, now=1_007))

    def test_retry_rejects_unstable_error_codes(self) -> None:
        self.store.enqueue(message(message_id=501, conversation_id=50), now=1_000)
        job = self.store.claim_next("worker-a", now=1_001, lease_seconds=300)
        self.assertIsNotNone(job)
        assert job is not None

        with self.assertRaisesRegex(ValueError, "error code"):
            self.store.schedule_retry(
                job.id,
                resume_stage="generating",
                error_code="contains remote body!",
                available_at=1_010,
                now=1_002,
            )

    def test_heartbeat_and_metrics_return_stable_integer_values(self) -> None:
        self.store.enqueue(message(message_id=601, conversation_id=60), now=1_000)
        self.store.record_heartbeat("worker-a", now=1_010)

        metrics = self.store.metrics(now=1_015)

        self.assertEqual(metrics.queued, 1)
        self.assertEqual(metrics.processing, 0)
        self.assertEqual(metrics.retry_wait, 0)
        self.assertEqual(metrics.failed, 0)
        self.assertEqual(metrics.oldest_wait_seconds, 15)
        self.assertEqual(metrics.last_worker_heartbeat, 1_010)
        self.assertTrue(
            all(
                isinstance(value, int)
                for value in (
                    metrics.queued,
                    metrics.processing,
                    metrics.retry_wait,
                    metrics.failed,
                    metrics.oldest_wait_seconds,
                    metrics.last_worker_heartbeat,
                )
            )
        )

    def test_reconciliation_cursor_initializes_at_now_and_advances_cas_style(self) -> None:
        self.assertIsNone(self.store.get_reconciliation_cursor())
        self.assertTrue(self.store.initialize_reconciliation_cursor(now=1_000))
        self.assertFalse(self.store.initialize_reconciliation_cursor(now=500))
        self.assertEqual(self.store.get_reconciliation_cursor(), 1_000)
        self.assertFalse(
            self.store.advance_reconciliation_cursor(expected=999, new_value=1_100)
        )
        self.assertTrue(
            self.store.advance_reconciliation_cursor(expected=1_000, new_value=1_100)
        )
        self.assertEqual(self.store.get_reconciliation_cursor(), 1_100)

    def test_dependency_status_exposes_only_health_fields(self) -> None:
        self.store.record_dependency_status(
            "dify", ok=True, error_code=None, checked_at=1_000
        )
        self.store.record_dependency_status(
            "chatwoot", ok=False, error_code="chatwoot_timeout", checked_at=1_001
        )

        self.assertEqual(
            self.store.dependency_statuses(),
            {
                "dify": {"ok": True, "error_code": None, "checked_at": 1_000},
                "chatwoot": {
                    "ok": False,
                    "error_code": "chatwoot_timeout",
                    "checked_at": 1_001,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
