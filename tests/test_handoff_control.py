import sqlite3
import unittest

from app.chatwoot_client import ChatwootAPIError
from app.handoff_control import (
    HandoffConflictError,
    HandoffControlService,
    HandoffNotFoundError,
    HandoffRemoteError,
    HandoffStorageError,
    ResumeResult,
    chatwoot_control_error_code,
)


class FakeResumeStore:
    def __init__(self) -> None:
        self.precheck = "ready"
        self.resume_status = "resumed"
        self.mode = "human"
        self.precheck_error: Exception | None = None
        self.resume_error: Exception | None = None
        self.failure_error: Exception | None = None
        self.resume_calls: list[tuple[int, int, int]] = []
        self.control_failures: list[tuple[int, str, str, int]] = []

    def get_resume_precheck(self, conversation_id: int) -> str:
        if self.precheck_error is not None:
            raise self.precheck_error
        return self.precheck

    def resume_ai_transaction(
        self,
        conversation_id: int,
        *,
        cutoff_message_id: int,
        now: int,
    ) -> str:
        self.resume_calls.append((conversation_id, cutoff_message_id, now))
        if self.resume_error is not None:
            raise self.resume_error
        if self.resume_status == "resumed":
            self.mode = "ai"
        return self.resume_status

    def record_control_failure(
        self,
        conversation_id: int,
        action: str,
        error_code: str,
        *,
        now: int,
    ) -> None:
        if self.failure_error is not None:
            raise self.failure_error
        self.control_failures.append((conversation_id, action, error_code, now))


class FakeControlChatwoot:
    def __init__(self) -> None:
        self.latest_id = 900
        self.labels = ("vip", "ai_handoff")
        self.latest_error: Exception | None = None
        self.remove_error: Exception | None = None
        self.calls: list[tuple[object, ...]] = []
        self.posted_label_updates = 0

    def latest_message_id(self, account_id: int, conversation_id: int) -> int:
        self.calls.append(("latest", account_id, conversation_id))
        if self.latest_error is not None:
            raise self.latest_error
        return self.latest_id

    def remove_label_preserving_existing(
        self,
        account_id: int,
        conversation_id: int,
        label: str,
    ) -> tuple[str, ...]:
        self.calls.append(("remove", account_id, conversation_id, label))
        if self.remove_error is not None:
            raise self.remove_error
        if label in self.labels:
            self.labels = tuple(item for item in self.labels if item != label)
            self.posted_label_updates += 1
        return self.labels


class HandoffControlServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeResumeStore()
        self.chatwoot = FakeControlChatwoot()
        self.service = HandoffControlService(
            store=self.store,
            chatwoot=self.chatwoot,
            account_id=1,
            base_url="http://127.0.0.1:3000/",
            human_sla_seconds=300,
            clock=lambda: 2_000,
        )

    def test_resume_ai_removes_label_before_atomic_local_resume(self) -> None:
        result = self.service.resume_ai(42)

        self.assertEqual(result, ResumeResult("resumed"))
        self.assertEqual(
            self.chatwoot.calls,
            [("latest", 1, 42), ("remove", 1, 42, "ai_handoff")],
        )
        self.assertEqual(self.store.resume_calls, [(42, 900, 2_000)])
        self.assertEqual(self.store.mode, "ai")

    def test_already_ai_is_idempotent_without_remote_calls_or_audit(self) -> None:
        self.store.precheck = "already_ai"
        self.store.mode = "ai"

        result = self.service.resume_ai(42)

        self.assertEqual(result, ResumeResult("already_ai"))
        self.assertEqual(self.chatwoot.calls, [])
        self.assertEqual(self.store.resume_calls, [])
        self.assertEqual(self.store.control_failures, [])

    def test_missing_and_blocking_conversations_stop_before_remote_calls(self) -> None:
        cases = (
            ("not_found", HandoffNotFoundError),
            ("conflict", HandoffConflictError),
        )
        for precheck, expected_error in cases:
            with self.subTest(precheck=precheck):
                self.store.precheck = precheck
                with self.assertRaises(expected_error):
                    self.service.resume_ai(42)
                self.assertEqual(self.chatwoot.calls, [])

    def test_chatwoot_failure_keeps_local_mode_human_and_records_stable_code(self) -> None:
        self.chatwoot.latest_error = ChatwootAPIError(503, "sensitive body")

        with self.assertRaises(HandoffRemoteError) as raised:
            self.service.resume_ai(42)

        self.assertEqual(raised.exception.error_code, "chatwoot_server_error")
        self.assertNotIn("sensitive body", str(raised.exception))
        self.assertEqual(self.store.mode, "human")
        self.assertEqual(
            self.store.control_failures,
            [(42, "ai_resumed", "chatwoot_server_error", 2_000)],
        )

    def test_local_conflict_after_label_removal_fails_safe(self) -> None:
        self.store.resume_status = "conflict"

        with self.assertRaises(HandoffConflictError):
            self.service.resume_ai(42)

        self.assertEqual(self.chatwoot.posted_label_updates, 1)
        self.assertEqual(self.store.mode, "human")

    def test_retry_after_label_was_already_removed_finishes_local_resume(self) -> None:
        self.chatwoot.labels = ("vip",)

        result = self.service.resume_ai(42)

        self.assertEqual(result.status, "resumed")
        self.assertEqual(self.chatwoot.posted_label_updates, 0)
        self.assertEqual(self.store.mode, "ai")

    def test_sqlite_errors_are_sanitized_as_storage_errors(self) -> None:
        self.store.precheck_error = sqlite3.OperationalError(
            "sensitive database path"
        )

        with self.assertRaises(HandoffStorageError) as raised:
            self.service.resume_ai(42)

        self.assertNotIn("sensitive database path", str(raised.exception))
        self.assertEqual(self.chatwoot.calls, [])

    def test_transaction_storage_error_after_label_removal_keeps_human_mode(self) -> None:
        self.store.resume_error = sqlite3.OperationalError("private db detail")

        with self.assertRaises(HandoffStorageError) as raised:
            self.service.resume_ai(42)

        self.assertNotIn("private db detail", str(raised.exception))
        self.assertEqual(self.chatwoot.posted_label_updates, 1)
        self.assertEqual(self.store.mode, "human")

    def test_audit_storage_error_takes_precedence_over_remote_error(self) -> None:
        self.chatwoot.remove_error = ChatwootAPIError(401, "remote body")
        self.store.failure_error = sqlite3.OperationalError("private db detail")

        with self.assertRaises(HandoffStorageError) as raised:
            self.service.resume_ai(42)

        self.assertNotIn("private db detail", str(raised.exception))
        self.assertNotIn("remote body", str(raised.exception))
        self.assertEqual(self.store.mode, "human")

    def test_chatwoot_control_error_codes_are_bounded(self) -> None:
        cases = (
            (401, "chatwoot_auth_error"),
            (403, "chatwoot_auth_error"),
            (429, "chatwoot_rate_limited"),
            (503, "chatwoot_server_error"),
            (None, "chatwoot_timeout"),
            (400, "chatwoot_request_error"),
        )
        for status_code, expected in cases:
            with self.subTest(status_code=status_code):
                self.assertEqual(
                    chatwoot_control_error_code(
                        ChatwootAPIError(status_code, "remote body")
                    ),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
