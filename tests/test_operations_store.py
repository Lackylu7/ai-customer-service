import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.conversation_store import SQLiteConversationStore
from app.operations_store import OperationsStore


class OperationsStoreMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "nested" / "state.db"
        self.store = OperationsStore(self.database_path)

    def test_records_event_without_customer_or_answer_text(self) -> None:
        self.store.record_event(
            message_id=101,
            conversation_id=9,
            outcome="replied",
            reason=None,
            duration_ms=125,
            created_at=1_720_000_000,
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT message_id, conversation_id, outcome, reason, "
                "duration_ms, created_at FROM service_events"
            ).fetchone()
            columns = {
                item[1]
                for item in connection.execute("PRAGMA table_info(service_events)")
            }

        self.assertEqual(row, (101, 9, "replied", None, 125, 1_720_000_000))
        self.assertNotIn("question", columns)
        self.assertNotIn("answer", columns)

    def test_rejects_invalid_event_fields_and_reason_combinations(self) -> None:
        invalid_events = (
            dict(message_id=0, conversation_id=9, outcome="replied", reason=None, duration_ms=1),
            dict(message_id=1, conversation_id=0, outcome="replied", reason=None, duration_ms=1),
            dict(message_id=1, conversation_id=9, outcome="unknown", reason=None, duration_ms=1),
            dict(message_id=1, conversation_id=9, outcome="replied", reason="dify_error", duration_ms=1),
            dict(message_id=1, conversation_id=9, outcome="handed_off", reason=None, duration_ms=1),
            dict(message_id=1, conversation_id=9, outcome="handed_off", reason="internal_error", duration_ms=1),
            dict(message_id=1, conversation_id=9, outcome="failed", reason="knowledge_gap", duration_ms=1),
            dict(message_id=1, conversation_id=9, outcome="failed", reason=None, duration_ms=1),
            dict(message_id=1, conversation_id=9, outcome="duplicate", reason=None, duration_ms=-1),
        )

        for event in invalid_events:
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.store.record_event(**event)

    def test_gap_upsert_merges_variants_and_preserves_manual_status(self) -> None:
        self.store.record_knowledge_gap(
            question=" ＡＢＣ  保修？ ",
            conversation_id=9,
            created_at=100,
        )
        gap = self.store.list_gaps(status="all")[0][0]
        updated = self.store.update_gap(
            gap.id,
            status="resolved",
            note="已补充保修说明",
            updated_at=150,
        )
        self.store.record_knowledge_gap(
            question="abc 保修",
            conversation_id=10,
            created_at=200,
        )

        merged = self.store.list_gaps(status="all")[0][0]

        self.assertIsNotNone(updated)
        self.assertEqual(merged.id, gap.id)
        self.assertEqual(merged.occurrence_count, 2)
        self.assertEqual(merged.first_seen_at, 100)
        self.assertEqual(merged.last_seen_at, 200)
        self.assertEqual(merged.last_conversation_id, 10)
        self.assertEqual(merged.status, "resolved")
        self.assertEqual(merged.note, "已补充保修说明")

    def test_gap_is_redacted_sorted_filtered_and_paginated(self) -> None:
        self.store.record_knowledge_gap(
            question="手机号 13812345678 怎么修改？",
            conversation_id=11,
            created_at=100,
        )
        self.store.record_knowledge_gap(
            question="退货运费谁承担？",
            conversation_id=12,
            created_at=200,
        )
        self.store.record_knowledge_gap(
            question="退货运费谁承担！",
            conversation_id=13,
            created_at=300,
        )

        page, total = self.store.list_gaps(status="pending", limit=1, offset=0)

        self.assertEqual(total, 2)
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0].sample_question, "退货运费谁承担？")
        phone_gap = self.store.list_gaps(status="all", limit=2, offset=1)[0][0]
        self.assertEqual(phone_gap.sample_question, "手机号 [手机号] 怎么修改？")

    def test_gap_update_validates_status_note_and_missing_id(self) -> None:
        self.store.record_knowledge_gap(
            question="保修多久？",
            conversation_id=9,
            created_at=100,
        )
        gap_id = self.store.list_gaps(status="all")[0][0].id

        with self.assertRaises(ValueError):
            self.store.update_gap(gap_id, status="closed", note="")
        with self.assertRaises(ValueError):
            self.store.update_gap(gap_id, status="pending", note="n" * 1001)
        with self.assertRaises(ValueError):
            self.store.list_gaps(status="all", limit=0)
        with self.assertRaises(ValueError):
            self.store.list_gaps(status="all", limit=201)
        with self.assertRaises(ValueError):
            self.store.list_gaps(status="all", offset=-1)
        self.assertIsNone(
            self.store.update_gap(999, status="ignored", note="不存在")
        )

    def test_initialization_preserves_existing_customer_service_tables(self) -> None:
        conversation_store = SQLiteConversationStore(self.database_path)
        conversation_store.save_conversation(
            42,
            dify_conversation_id="dify-42",
            mode="ai",
        )

        OperationsStore(self.database_path)

        self.assertEqual(
            conversation_store.get_conversation(42).dify_conversation_id,
            "dify-42",
        )


class OperationsStoreSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "state.db"
        self.store = OperationsStore(self.database_path)
        self.now = 1_720_000_100

    def record(
        self,
        message_id: int,
        outcome: str,
        *,
        reason: str | None = None,
        duration_ms: int = 100,
        created_at: int | None = None,
    ) -> None:
        self.store.record_event(
            message_id=message_id,
            conversation_id=message_id + 100,
            outcome=outcome,
            reason=reason,
            duration_ms=duration_ms,
            created_at=self.now - 100 if created_at is None else created_at,
        )

    def test_summary_uses_last_non_duplicate_result_per_message(self) -> None:
        self.record(1, "failed", reason="chatwoot_error", created_at=self.now - 200)
        self.record(1, "replied", duration_ms=100, created_at=self.now - 90)
        self.record(
            2,
            "handed_off",
            reason="knowledge_gap",
            duration_ms=200,
            created_at=self.now - 80,
        )
        self.record(3, "ignored_human_mode", duration_ms=10, created_at=self.now - 70)
        self.record(
            4,
            "handed_off",
            reason="dify_error",
            duration_ms=300,
            created_at=self.now - 60,
        )
        self.record(1, "duplicate", duration_ms=1, created_at=self.now - 50)

        summary = self.store.get_summary("7d", now=self.now)

        self.assertEqual(summary["unique_customer_messages"], 4)
        self.assertEqual(summary["ai_eligible_messages"], 3)
        self.assertEqual(summary["replied"], 1)
        self.assertEqual(summary["handed_off"], 2)
        self.assertEqual(summary["ignored_human_mode"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["system_errors"], 1)
        self.assertEqual(summary["duplicate_webhooks"], 1)
        self.assertEqual(summary["average_ai_duration_ms"], 200)
        self.assertEqual(
            summary["answer_rate"],
            {"numerator": 1, "denominator": 3, "value": 1 / 3},
        )
        self.assertEqual(
            summary["handoff_rate"],
            {"numerator": 2, "denominator": 3, "value": 2 / 3},
        )
        self.assertEqual(
            summary["knowledge_gap_rate"],
            {"numerator": 1, "denominator": 3, "value": 1 / 3},
        )
        self.assertEqual(summary["handoff_reasons"]["knowledge_gap"], 1)
        self.assertEqual(summary["handoff_reasons"]["dify_error"], 1)
        self.assertEqual(summary["handoff_reasons"]["explicit_human_request"], 0)
        self.assertEqual(len(summary["trend"]), 1)
        self.assertEqual(summary["trend"][0]["customer_messages"], 4)
        self.assertEqual(summary["trend"][0]["knowledge_gaps"], 1)

    def test_empty_summary_returns_zero_percentages_and_latency(self) -> None:
        summary = self.store.get_summary("all", now=self.now)

        for metric in ("answer_rate", "handoff_rate", "knowledge_gap_rate"):
            self.assertEqual(
                summary[metric],
                {"numerator": 0, "denominator": 0, "value": 0},
            )
        self.assertEqual(summary["average_ai_duration_ms"], 0)
        self.assertEqual(summary["trend"], [])

    def test_time_window_filters_by_final_event_timestamp(self) -> None:
        eight_days_ago = self.now - 8 * 86_400
        self.record(7, "replied", created_at=eight_days_ago)

        self.assertEqual(self.store.get_summary("7d", now=self.now)["replied"], 0)
        self.assertEqual(self.store.get_summary("30d", now=self.now)["replied"], 1)
        self.assertEqual(self.store.get_summary("all", now=self.now)["replied"], 1)

    def test_rejects_invalid_window(self) -> None:
        with self.assertRaises(ValueError):
            self.store.get_summary("yesterday", now=self.now)

    def test_exports_raw_events_and_windowed_gaps_in_stable_order(self) -> None:
        self.record(1, "failed", reason="internal_error", created_at=self.now - 20)
        self.record(1, "replied", created_at=self.now - 10)
        self.store.record_knowledge_gap(
            question="保修承诺？",
            conversation_id=101,
            created_at=self.now - 15,
        )

        events = self.store.export_events("7d", now=self.now)
        gaps = self.store.export_gaps("7d", now=self.now)

        self.assertEqual([event["outcome"] for event in events], ["replied", "failed"])
        self.assertEqual(
            tuple(events[0]),
            (
                "id",
                "message_id",
                "conversation_id",
                "outcome",
                "reason",
                "duration_ms",
                "created_at",
            ),
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["sample_question"], "保修承诺？")
        self.assertNotIn("fingerprint", gaps[0])


if __name__ == "__main__":
    unittest.main()
