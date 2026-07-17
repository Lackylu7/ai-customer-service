import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.chatwoot_events import HumanReply, IncomingMessage
from app.job_models import PreparedAction
from app.job_store import SQLiteJobStore


def message(
    *,
    message_id: int,
    conversation_id: int,
    created_at: int = 0,
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        account_id=1,
        conversation_id=conversation_id,
        sender_id=99,
        content=f"问题 {message_id}",
        labels=("vip",),
        created_at=created_at,
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
        self.assertEqual(job_one.claimed_from_stage, "queued")
        self.assertEqual(job_one.received_at, 1_000)
        self.assertEqual(job_one.message_created_at, 1_000)
        self.assertEqual(job_one.job_kind, "standard")
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

    def test_human_wait_survives_reopen_and_does_not_increment_generation_attempts(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO conversation_states (
                    conversation_id, dify_conversation_id, mode
                ) VALUES (42, NULL, 'human')
                """
            )
            connection.commit()
        self.store.enqueue(
            message(message_id=701, conversation_id=42, created_at=1_000),
            now=1_010,
        )
        claimed = self.store.claim_next(
            "worker-a",
            now=1_010,
            lease_seconds=300,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None

        self.assertTrue(
            self.store.defer_human_wait(
                claimed.id,
                available_at=1_310,
                now=1_011,
            )
        )

        reopened = SQLiteJobStore(self.database_path)
        self.assertIsNone(
            reopened.claim_next(
                "worker-a",
                now=1_309,
                lease_seconds=300,
            )
        )
        due = reopened.claim_next(
            "worker-a",
            now=1_310,
            lease_seconds=300,
        )
        self.assertIsNotNone(due)
        assert due is not None
        self.assertEqual(due.stage, "generating")
        self.assertEqual(due.claimed_from_stage, "human_wait")
        self.assertEqual(due.job_kind, "human_pending")
        self.assertEqual(due.outgoing_source_id, "ai-cs:701:human-fallback")
        self.assertEqual(due.generation_attempts, 1)

    def test_queue_metrics_count_human_wait_as_waiting(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO conversation_states (
                    conversation_id, dify_conversation_id, mode
                ) VALUES (43, NULL, 'human')
                """
            )
            connection.commit()
        self.store.enqueue(
            message(message_id=702, conversation_id=43, created_at=1_000),
            now=1_010,
        )
        claimed = self.store.claim_next(
            "worker-a",
            now=1_010,
            lease_seconds=300,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.store.defer_human_wait(
            claimed.id,
            available_at=1_310,
            now=1_011,
        )

        metrics = self.store.metrics(now=1_100)

        self.assertEqual(metrics.queued, 1)
        self.assertEqual(metrics.oldest_wait_seconds, 90)

    def test_human_reply_cancels_waiting_fallback_and_completes_message(self) -> None:
        self._seed_human_conversation(44)
        self.store.enqueue(
            message(message_id=703, conversation_id=44, created_at=1_000),
            now=1_010,
        )
        claimed = self.store.claim_next(
            "worker-a",
            now=1_010,
            lease_seconds=300,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.store.defer_human_wait(
            claimed.id,
            available_at=1_310,
            now=1_011,
        )

        cancelled = self.store.record_human_reply(
            HumanReply(
                message_id=900,
                account_id=1,
                conversation_id=44,
                created_at=1_100,
            ),
            now=1_101,
        )

        self.assertEqual(cancelled, 1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            job = connection.execute(
                """
                SELECT stage FROM inbound_jobs WHERE message_id=703
                """
            ).fetchone()
            processed = connection.execute(
                """
                SELECT status FROM processed_messages WHERE message_id=703
                """
            ).fetchone()
            state = connection.execute(
                """
                SELECT last_human_reply_message_id, last_human_reply_at
                FROM conversation_states WHERE conversation_id=44
                """
            ).fetchone()
        self.assertEqual(job, ("completed",))
        self.assertEqual(processed, ("completed",))
        self.assertEqual(state, (900, 1_100))

    def test_fallback_cooldown_completes_due_job_without_delivery(self) -> None:
        self._seed_human_conversation(45, last_fallback_at=1_200)
        self.store.enqueue(
            message(message_id=704, conversation_id=45, created_at=1_000),
            now=1_010,
        )
        claimed = self.store.claim_next(
            "worker-a",
            now=1_010,
            lease_seconds=300,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.store.defer_human_wait(
            claimed.id,
            available_at=1_310,
            now=1_011,
        )
        due = self.store.claim_next(
            "worker-a",
            now=1_310,
            lease_seconds=300,
        )
        self.assertIsNotNone(due)
        assert due is not None

        eligible = self.store.confirm_human_fallback(
            due.id,
            cooldown_seconds=300,
            now=1_310,
        )

        self.assertFalse(eligible)
        with closing(sqlite3.connect(self.database_path)) as connection:
            stage = connection.execute(
                "SELECT stage FROM inbound_jobs WHERE id=?",
                (due.id,),
            ).fetchone()[0]
        self.assertEqual(stage, "completed")

    def test_human_fallback_completion_updates_cooldown_and_audit(self) -> None:
        self._seed_human_conversation(46)
        self.store.enqueue(
            message(message_id=705, conversation_id=46, created_at=1_000),
            now=1_010,
        )
        claimed = self.store.claim_next(
            "worker-a",
            now=1_010,
            lease_seconds=300,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.store.defer_human_wait(
            claimed.id,
            available_at=1_310,
            now=1_011,
        )
        due = self.store.claim_next(
            "worker-a",
            now=1_310,
            lease_seconds=300,
        )
        self.assertIsNotNone(due)
        assert due is not None
        self.assertTrue(
            self.store.confirm_human_fallback(
                due.id,
                cooldown_seconds=300,
                now=1_310,
            )
        )
        self.assertTrue(
            self.store.save_human_fallback_prepared(
                due.id,
                PreparedAction.customer_message("AI 临时协助：48 小时内发货。"),
                job_kind="human_timeout_reply",
                processing_duration_ms=50,
                now=1_311,
            )
        )
        delivery = self.store.claim_next(
            "worker-a",
            now=1_312,
            lease_seconds=300,
        )
        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.assertEqual(delivery.job_kind, "human_timeout_reply")
        self.assertTrue(
            self.store.can_deliver_human_fallback(
                delivery.id,
                now=1_313,
            )
        )

        self.store.complete_human_fallback(delivery.id, now=1_314)

        with closing(sqlite3.connect(self.database_path)) as connection:
            state = connection.execute(
                """
                SELECT mode, last_fallback_at
                FROM conversation_states WHERE conversation_id=46
                """
            ).fetchone()
            audit = connection.execute(
                """
                SELECT action, result, error_code
                FROM conversation_control_events
                WHERE conversation_id=46
                """
            ).fetchone()
        self.assertEqual(state, ("human", 1_314))
        self.assertEqual(audit, ("human_timeout_reply", "succeeded", None))

    def test_human_fallback_terminal_failure_records_sanitized_audit(self) -> None:
        self._seed_human_conversation(47)
        self.store.enqueue(
            message(message_id=706, conversation_id=47, created_at=1_000),
            now=1_010,
        )
        claimed = self.store.claim_next("worker-a", now=1_010, lease_seconds=300)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.store.defer_human_wait(claimed.id, available_at=1_310, now=1_011)
        due = self.store.claim_next("worker-a", now=1_310, lease_seconds=300)
        self.assertIsNotNone(due)
        assert due is not None
        self.assertTrue(
            self.store.confirm_human_fallback(
                due.id,
                cooldown_seconds=300,
                now=1_310,
            )
        )
        self.assertTrue(
            self.store.save_human_fallback_prepared(
                due.id,
                PreparedAction.customer_message("人工客服暂时未能及时回复，请稍候。"),
                job_kind="human_wait_notice",
                processing_duration_ms=10,
                now=1_311,
            )
        )
        delivery = self.store.claim_next(
            "worker-a",
            now=1_312,
            lease_seconds=300,
        )
        self.assertIsNotNone(delivery)
        assert delivery is not None

        self.store.fail(
            delivery.id,
            resume_stage="delivering",
            error_code="chatwoot_auth_error",
            now=1_313,
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            stage = connection.execute(
                "SELECT stage FROM inbound_jobs WHERE id=?",
                (delivery.id,),
            ).fetchone()
            audit = connection.execute(
                """
                SELECT action, result, error_code
                FROM conversation_control_events
                WHERE conversation_id=47
                """
            ).fetchone()
        self.assertEqual(stage, ("failed",))
        self.assertEqual(
            audit,
            ("human_wait_notice", "failed", "chatwoot_auth_error"),
        )

    def test_resume_ai_transaction_completes_safe_jobs_and_sets_cutoff(self) -> None:
        self._seed_human_conversation(48)
        waiting_result = self.store.enqueue(
            message(message_id=707, conversation_id=48, created_at=1_000),
            now=1_010,
        )
        waiting = self.store.claim_next(
            "worker-a",
            now=1_010,
            lease_seconds=300,
        )
        self.assertIsNotNone(waiting)
        assert waiting is not None
        self.store.defer_human_wait(waiting.id, available_at=1_310, now=1_011)
        queued_result = self.store.enqueue(
            message(message_id=708, conversation_id=48, created_at=1_020),
            now=1_020,
        )

        self.assertEqual(self.store.get_resume_precheck(48), "ready")
        status = self.store.resume_ai_transaction(
            48,
            cutoff_message_id=900,
            now=2_000,
        )

        self.assertEqual(status, "resumed")
        with closing(sqlite3.connect(self.database_path)) as connection:
            state = connection.execute(
                """
                SELECT mode, dify_conversation_id, handoff_reason, human_since,
                       last_customer_message_id, last_customer_message_at,
                       last_human_reply_message_id, last_human_reply_at,
                       last_fallback_at, ai_resume_cutoff_message_id
                FROM conversation_states WHERE conversation_id=48
                """
            ).fetchone()
            jobs = connection.execute(
                """
                SELECT message_id, stage FROM inbound_jobs
                WHERE conversation_id=48 ORDER BY message_id
                """
            ).fetchall()
            processed = connection.execute(
                """
                SELECT message_id, status FROM processed_messages
                WHERE message_id IN (707, 708) ORDER BY message_id
                """
            ).fetchall()
            audit = connection.execute(
                """
                SELECT action, result, error_code
                FROM conversation_control_events WHERE conversation_id=48
                """
            ).fetchone()
        self.assertEqual(state, ("ai", None, None, None, None, None, None, None, None, 900))
        self.assertEqual(jobs, [(707, "completed"), (708, "completed")])
        self.assertEqual(processed, [(707, "completed"), (708, "completed")])
        self.assertEqual(audit, ("ai_resumed", "succeeded", None))
        self.assertIsNotNone(waiting_result.job_id)
        self.assertIsNotNone(queued_result.job_id)

    def test_resume_ai_transaction_rejects_every_blocking_job_stage(self) -> None:
        blocking_stages = (
            "generating",
            "ready_to_deliver",
            "delivering",
            "retry_wait",
            "failed",
        )
        for index, stage in enumerate(blocking_stages, start=1):
            with self.subTest(stage=stage):
                database_path = Path(self.temp_dir.name) / f"resume-{stage}.db"
                store = SQLiteJobStore(database_path)
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute(
                        """
                        INSERT INTO conversation_states (
                            conversation_id, dify_conversation_id, mode
                        ) VALUES (49, NULL, 'human')
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO inbound_jobs (
                            message_id, account_id, conversation_id, sender_id,
                            content, labels_json, stage, resume_stage,
                            outgoing_source_id, message_created_at,
                            available_at, created_at, updated_at
                        ) VALUES (?, 1, 49, 9, '问题', '[]', ?, ?, ?, 1000, 1000, 1000, 1000)
                        """,
                        (
                            800 + index,
                            stage,
                            "generating" if stage in {"retry_wait", "failed"} else None,
                            f"ai-cs:{800 + index}:reply",
                        ),
                    )
                    connection.commit()

                self.assertEqual(store.get_resume_precheck(49), "conflict")
                self.assertEqual(
                    store.resume_ai_transaction(
                        49,
                        cutoff_message_id=900,
                        now=2_000,
                    ),
                    "conflict",
                )
                with closing(sqlite3.connect(database_path)) as connection:
                    mode = connection.execute(
                        "SELECT mode FROM conversation_states WHERE conversation_id=49"
                    ).fetchone()
                    audit_count = connection.execute(
                        "SELECT COUNT(*) FROM conversation_control_events"
                    ).fetchone()[0]
                self.assertEqual(mode, ("human",))
                self.assertEqual(audit_count, 0)

    def test_resume_ai_transaction_is_idempotent_and_handles_missing_state(self) -> None:
        self.assertEqual(self.store.get_resume_precheck(999), "not_found")
        self.assertEqual(
            self.store.resume_ai_transaction(
                999,
                cutoff_message_id=900,
                now=2_000,
            ),
            "not_found",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO conversation_states (
                    conversation_id, dify_conversation_id, mode,
                    ai_resume_cutoff_message_id
                ) VALUES (50, NULL, 'ai', 850)
                """
            )
            connection.commit()

        self.assertEqual(self.store.get_resume_precheck(50), "already_ai")
        self.assertEqual(
            self.store.resume_ai_transaction(
                50,
                cutoff_message_id=900,
                now=2_000,
            ),
            "already_ai",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            state = connection.execute(
                """
                SELECT mode, ai_resume_cutoff_message_id
                FROM conversation_states WHERE conversation_id=50
                """
            ).fetchone()
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM conversation_control_events WHERE conversation_id=50"
            ).fetchone()[0]
        self.assertEqual(state, ("ai", 850))
        self.assertEqual(audit_count, 0)

    def test_record_control_failure_stores_only_stable_error_code(self) -> None:
        self._seed_human_conversation(51)

        self.store.record_control_failure(
            51,
            "ai_resumed",
            "chatwoot_server_error",
            now=2_000,
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            audit = connection.execute(
                """
                SELECT action, result, error_code
                FROM conversation_control_events WHERE conversation_id=51
                """
            ).fetchone()
        self.assertEqual(
            audit,
            ("ai_resumed", "failed", "chatwoot_server_error"),
        )

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

    def test_enqueue_rejects_message_at_or_before_ai_resume_cutoff(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO conversation_states (
                    conversation_id, dify_conversation_id, mode,
                    ai_resume_cutoff_message_id
                ) VALUES (82, NULL, 'ai', 900)
                """
            )
            connection.commit()

        result = self.store.enqueue(
            message(message_id=899, conversation_id=82, created_at=1_000),
            now=1_100,
        )

        self.assertEqual((result.status, result.job_id), ("duplicate", None))
        with closing(sqlite3.connect(self.database_path)) as connection:
            jobs = connection.execute(
                "SELECT COUNT(*) FROM inbound_jobs WHERE conversation_id=82"
            ).fetchone()[0]
            processed = connection.execute(
                "SELECT status FROM processed_messages WHERE message_id=899"
            ).fetchone()
        self.assertEqual(jobs, 0)
        self.assertEqual(processed, ("completed",))

    def test_enqueue_updates_latest_customer_activity_in_human_mode(self) -> None:
        self._seed_human_conversation(83)

        self.store.enqueue(
            message(message_id=901, conversation_id=83, created_at=1_005),
            now=1_100,
        )
        self.store.enqueue(
            message(message_id=900, conversation_id=83, created_at=1_004),
            now=1_101,
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            state = connection.execute(
                """
                SELECT last_customer_message_id, last_customer_message_at
                FROM conversation_states WHERE conversation_id=83
                """
            ).fetchone()
        self.assertEqual(state, (901, 1_005))

    def test_legacy_job_table_is_rebuilt_without_losing_jobs(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-jobs.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
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
                        'queued','generating','ready_to_deliver','delivering',
                        'retry_wait','completed','failed'
                    )),
                    resume_stage TEXT CHECK(resume_stage IN (
                        'generating','delivering'
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
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                INSERT INTO inbound_jobs (
                    message_id, account_id, conversation_id, sender_id,
                    content, labels_json, stage, outgoing_source_id,
                    generation_attempts, delivery_attempts,
                    available_at, created_at, updated_at
                ) VALUES (
                    801, 1, 80, 9, 'queued question', '[]', 'queued',
                    'ai-cs:801:reply', 0, 0, 1000, 1000, 1000
                );
                INSERT INTO inbound_jobs (
                    message_id, account_id, conversation_id, sender_id,
                    content, labels_json, stage, resume_stage,
                    outgoing_source_id, generation_attempts,
                    delivery_attempts, available_at, last_error_code,
                    created_at, updated_at
                ) VALUES (
                    802, 1, 81, 9, 'failed question', '[]', 'failed',
                    'generating', 'ai-cs:802:reply', 2, 0, 1001,
                    'dify_auth_error', 1001, 1002
                );
                """
            )
            connection.commit()

        migrated = SQLiteJobStore(legacy_path)

        with closing(sqlite3.connect(legacy_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(inbound_jobs)")
            }
            rows = connection.execute(
                """
                SELECT message_id, stage, resume_stage, job_kind,
                       message_created_at, generation_attempts,
                       last_error_code
                FROM inbound_jobs
                ORDER BY message_id
                """
            ).fetchall()
            table_sql = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type='table' AND name='inbound_jobs'
                """
            ).fetchone()[0]

        self.assertIsInstance(migrated, SQLiteJobStore)
        self.assertIn("job_kind", columns)
        self.assertIn("message_created_at", columns)
        self.assertIn("'human_wait'", table_sql)
        self.assertEqual(
            rows,
            [
                (801, "queued", None, "standard", 1_000, 0, None),
                (
                    802,
                    "failed",
                    "generating",
                    "standard",
                    1_001,
                    2,
                    "dify_auth_error",
                ),
            ],
        )

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

    def _seed_human_conversation(
        self,
        conversation_id: int,
        *,
        last_fallback_at: int | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO conversation_states (
                    conversation_id, dify_conversation_id, mode,
                    last_fallback_at
                ) VALUES (?, NULL, 'human', ?)
                """,
                (conversation_id, last_fallback_at),
            )
            connection.commit()


if __name__ == "__main__":
    unittest.main()
