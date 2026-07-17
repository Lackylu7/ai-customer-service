import unittest
from dataclasses import dataclass

from app.chatwoot_client import ChatwootAPIError
from app.customer_service import HandoffLabelError
from app.dify_chat_client import DifyChatAPIError
from app.job_models import DurableJob, PreparedAction
from app.job_processor import JobProcessor, retry_delay_seconds
from app.settings import WorkerSettings


@dataclass(frozen=True)
class RetryCall:
    job_id: int
    resume_stage: str
    error_code: str
    available_at: int
    now: int


@dataclass(frozen=True)
class FailCall:
    job_id: int
    resume_stage: str
    error_code: str
    now: int


@dataclass(frozen=True)
class FakeFallbackPreparation:
    action: PreparedAction
    job_kind: str
    dify_used: bool


class FakeJobStore:
    def __init__(self) -> None:
        self.saved_actions: list[PreparedAction] = []
        self.saved_durations: list[int] = []
        self.retries: list[RetryCall] = []
        self.failures: list[FailCall] = []
        self.completed_job_ids: list[int] = []
        self.deferred: list[tuple[int, int, int]] = []
        self.confirmed_fallbacks: list[tuple[int, int, int]] = []
        self.confirm_fallback_result = True
        self.saved_fallbacks: list[FakeFallbackPreparation] = []
        self.can_deliver_fallback_result = True
        self.can_deliver_fallback_calls: list[tuple[int, int]] = []
        self.completed_fallbacks: list[tuple[int, int]] = []
        self.dependencies: list[dict[str, object]] = []

    def save_prepared(
        self,
        job_id: int,
        action: PreparedAction,
        *,
        processing_duration_ms: int,
        now: int,
    ) -> None:
        self.saved_actions.append(action)
        self.saved_durations.append(processing_duration_ms)

    def schedule_retry(
        self,
        job_id: int,
        *,
        resume_stage: str,
        error_code: str,
        available_at: int,
        now: int,
    ) -> None:
        self.retries.append(
            RetryCall(job_id, resume_stage, error_code, available_at, now)
        )

    def fail(
        self,
        job_id: int,
        *,
        resume_stage: str,
        error_code: str,
        now: int,
    ) -> None:
        self.failures.append(FailCall(job_id, resume_stage, error_code, now))

    def complete(self, job_id: int, *, now: int) -> None:
        self.completed_job_ids.append(job_id)

    def defer_human_wait(
        self,
        job_id: int,
        *,
        available_at: int,
        now: int,
    ) -> bool:
        self.deferred.append((job_id, available_at, now))
        return True

    def confirm_human_fallback(
        self,
        job_id: int,
        *,
        cooldown_seconds: int,
        now: int,
    ) -> bool:
        self.confirmed_fallbacks.append((job_id, cooldown_seconds, now))
        return self.confirm_fallback_result

    def save_human_fallback_prepared(
        self,
        job_id: int,
        action: PreparedAction,
        *,
        job_kind: str,
        processing_duration_ms: int,
        now: int,
    ) -> bool:
        self.saved_fallbacks.append(
            FakeFallbackPreparation(action, job_kind, dify_used=False)
        )
        self.saved_durations.append(processing_duration_ms)
        return True

    def can_deliver_human_fallback(self, job_id: int, *, now: int) -> bool:
        self.can_deliver_fallback_calls.append((job_id, now))
        return self.can_deliver_fallback_result

    def complete_human_fallback(self, job_id: int, *, now: int) -> None:
        self.completed_fallbacks.append((job_id, now))

    def record_dependency_status(
        self,
        name: str,
        *,
        ok: bool,
        error_code: str | None,
        checked_at: int,
    ) -> None:
        self.dependencies.append(
            {
                "name": name,
                "ok": ok,
                "error_code": error_code,
                "checked_at": checked_at,
            }
        )


class FakeCustomerService:
    def __init__(self) -> None:
        self.prepare_calls: list[object] = []
        self.fallback_prepare_calls: list[object] = []
        self.delivery_calls: list[tuple[object, PreparedAction, str]] = []
        self.fallback_delivery_calls: list[tuple[object, PreparedAction, str]] = []
        self.result_calls: list[tuple[object, PreparedAction, int]] = []
        self.prepare_error: Exception | None = None
        self.fallback_prepare_error: Exception | None = None
        self.delivery_error: Exception | None = None
        self.prepared_action = PreparedAction.reply("48 小时内发货。", "dify-7")
        self.fallback_preparation = FakeFallbackPreparation(
            PreparedAction.customer_message("AI 临时协助：48 小时内发货。"),
            "human_timeout_reply",
            True,
        )

    def prepare(self, message: object) -> PreparedAction:
        self.prepare_calls.append(message)
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.prepared_action

    def dify_error_action(self) -> PreparedAction:
        return PreparedAction.handoff("正在转接人工。", "dify_error")

    def prepare_human_fallback(self, message: object) -> FakeFallbackPreparation:
        self.fallback_prepare_calls.append(message)
        if self.fallback_prepare_error is not None:
            raise self.fallback_prepare_error
        return self.fallback_preparation

    def human_wait_text(self) -> str:
        return "人工客服暂时未能及时回复，请稍候。"

    def deliver(
        self,
        message: object,
        action: PreparedAction,
        *,
        source_id: str,
    ) -> None:
        self.delivery_calls.append((message, action, source_id))
        if self.delivery_error is not None:
            raise self.delivery_error

    def record_result(
        self,
        message: object,
        action: PreparedAction,
        duration_ms: int,
    ) -> None:
        self.result_calls.append((message, action, duration_ms))

    def deliver_human_fallback(
        self,
        message: object,
        action: PreparedAction,
        *,
        source_id: str,
    ) -> None:
        self.fallback_delivery_calls.append((message, action, source_id))
        if self.delivery_error is not None:
            raise self.delivery_error


class FakeMonotonic:
    def __init__(self) -> None:
        self.values = iter((10.0, 10.125))

    def __call__(self) -> float:
        return next(self.values)


def durable_job(
    *,
    stage: str,
    generation_attempts: int = 0,
    delivery_attempts: int = 0,
    action: PreparedAction | None = None,
    claimed_from_stage: str | None = None,
    received_at: int = 1_000,
    job_kind: str = "standard",
) -> DurableJob:
    return DurableJob(
        id=55,
        message_id=101,
        account_id=1,
        conversation_id=42,
        sender_id=9,
        content="现货多久发货？",
        labels=("vip",),
        stage=stage,
        resume_stage=None,
        action=action,
        outgoing_source_id=(
            "ai-cs:101:reply"
            if job_kind == "standard"
            else "ai-cs:101:human-fallback"
        ),
        generation_attempts=generation_attempts,
        delivery_attempts=delivery_attempts,
        processing_duration_ms=125,
        available_at=1_000,
        locked_at=1_000,
        locked_by="worker-a",
        last_error_code=None,
        claimed_from_stage=claimed_from_stage,
        message_created_at=received_at,
        received_at=received_at,
        job_kind=job_kind,
    )


class JobProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeJobStore()
        self.service = FakeCustomerService()
        self.processor = JobProcessor(
            service=self.service,
            store=self.store,
            settings=WorkerSettings.from_env({}),
            clock=lambda: 1_000,
            monotonic=FakeMonotonic(),
        )

    def test_generation_persists_action_without_delivering(self) -> None:
        self.processor.process(
            durable_job(stage="generating", generation_attempts=1)
        )

        self.assertEqual(self.store.saved_actions, [self.service.prepared_action])
        self.assertEqual(self.store.saved_durations, [125])
        self.assertEqual(self.service.delivery_calls, [])
        self.assertEqual(self.store.dependencies[-1]["ok"], True)

    def test_human_mode_generation_is_deferred_until_sla(self) -> None:
        self.service.prepared_action = PreparedAction.ignore_human_mode()

        self.processor.process(
            durable_job(
                stage="generating",
                claimed_from_stage="queued",
                received_at=1_000,
            )
        )

        self.assertEqual(self.store.deferred, [(55, 1_300, 1_000)])
        self.assertEqual(self.store.saved_actions, [])
        self.assertEqual(self.store.dependencies, [])

    def test_due_human_wait_prepares_fallback_without_standard_prepare(self) -> None:
        self.processor.process(
            durable_job(
                stage="generating",
                claimed_from_stage="human_wait",
                job_kind="human_pending",
                generation_attempts=1,
            )
        )

        self.assertEqual(self.service.prepare_calls, [])
        self.assertEqual(len(self.service.fallback_prepare_calls), 1)
        self.assertEqual(
            self.store.saved_fallbacks[0].job_kind,
            "human_timeout_reply",
        )
        self.assertEqual(self.store.confirmed_fallbacks, [(55, 300, 1_000)])
        self.assertEqual(self.store.dependencies[-1]["ok"], True)

    def test_expired_human_fallback_lease_resumes_fallback_path(self) -> None:
        self.processor.process(
            durable_job(
                stage="generating",
                claimed_from_stage="retry_wait",
                job_kind="human_pending",
                generation_attempts=1,
            )
        )

        self.assertEqual(self.service.prepare_calls, [])
        self.assertEqual(len(self.service.fallback_prepare_calls), 1)

    def test_dify_error_during_human_fallback_uses_wait_notice_without_retry(self) -> None:
        self.service.fallback_prepare_error = DifyChatAPIError(
            503,
            "sensitive remote body",
        )

        self.processor.process(
            durable_job(stage="generating", job_kind="human_pending")
        )

        saved = self.store.saved_fallbacks[0]
        self.assertEqual(saved.job_kind, "human_wait_notice")
        self.assertEqual(saved.action.customer_text, self.service.human_wait_text())
        self.assertEqual(self.store.retries, [])
        self.assertEqual(self.store.dependencies[-1]["error_code"], "dify_server_error")
        self.assertNotIn("sensitive remote body", repr(self.store.dependencies))

    def test_transient_dify_error_schedules_generation_retry(self) -> None:
        self.service.prepare_error = DifyChatAPIError(None, "timeout detail")

        self.processor.process(
            durable_job(stage="generating", generation_attempts=1)
        )

        self.assertEqual(self.store.retries[0].resume_stage, "generating")
        self.assertEqual(self.store.retries[0].error_code, "dify_timeout")
        self.assertEqual(self.store.retries[0].available_at, 1_005)
        self.assertNotIn("timeout detail", repr(self.store.retries[0]))

    def test_exhausted_dify_error_persists_safe_handoff(self) -> None:
        self.service.prepare_error = DifyChatAPIError(503, "remote body")

        self.processor.process(
            durable_job(stage="generating", generation_attempts=2)
        )

        self.assertEqual(self.store.saved_actions[-1].reason, "dify_error")
        self.assertEqual(self.store.retries, [])

    def test_delivery_never_prepares_again_and_completes(self) -> None:
        action = PreparedAction.reply("48 小时内发货。", "dify-7")

        self.processor.process(
            durable_job(stage="delivering", delivery_attempts=1, action=action)
        )

        self.assertEqual(self.service.prepare_calls, [])
        self.assertEqual(self.store.completed_job_ids, [55])
        self.assertEqual(self.service.delivery_calls[0][2], "ai-cs:101:reply")
        self.assertEqual(self.service.result_calls[0][2], 125)

    def test_human_reply_before_fallback_delivery_prevents_send(self) -> None:
        self.store.can_deliver_fallback_result = False

        self.processor.process(
            durable_job(
                stage="delivering",
                action=PreparedAction.customer_message("AI 临时协助：答复"),
                job_kind="human_timeout_reply",
            )
        )

        self.assertEqual(self.service.fallback_delivery_calls, [])
        self.assertEqual(self.store.completed_fallbacks, [])
        self.assertEqual(self.store.can_deliver_fallback_calls, [(55, 1_000)])

    def test_human_fallback_delivery_keeps_standard_result_path_unused(self) -> None:
        action = PreparedAction.customer_message("AI 临时协助：答复")

        self.processor.process(
            durable_job(
                stage="delivering",
                action=action,
                job_kind="human_timeout_reply",
            )
        )

        self.assertEqual(self.service.delivery_calls, [])
        self.assertEqual(self.service.result_calls, [])
        self.assertEqual(self.store.completed_job_ids, [])
        self.assertEqual(self.store.completed_fallbacks, [(55, 1_000)])
        self.assertEqual(
            self.service.fallback_delivery_calls[0][2],
            "ai-cs:101:human-fallback",
        )

    def test_chatwoot_auth_error_fails_without_retry(self) -> None:
        self.service.delivery_error = ChatwootAPIError(401, "secret remote text")

        self.processor.process(
            durable_job(
                stage="delivering",
                delivery_attempts=1,
                action=PreparedAction.reply("答复", "dify-7"),
            )
        )

        self.assertEqual(self.store.failures[0].error_code, "chatwoot_auth_error")
        self.assertEqual(self.store.retries, [])
        self.assertNotIn("secret remote text", repr(self.store.failures[0]))

    def test_transient_chatwoot_error_schedules_delivery_retry(self) -> None:
        self.service.delivery_error = ChatwootAPIError(503, "unavailable")

        self.processor.process(
            durable_job(
                stage="delivering",
                delivery_attempts=1,
                action=PreparedAction.reply("答复", "dify-7"),
            )
        )

        self.assertEqual(self.store.retries[0].resume_stage, "delivering")
        self.assertEqual(self.store.retries[0].error_code, "chatwoot_server_error")
        self.assertEqual(self.store.retries[0].available_at, 1_005)

    def test_label_error_has_stable_code_and_delivery_cap(self) -> None:
        cause = ChatwootAPIError(503, "remote label body")
        self.service.delivery_error = HandoffLabelError(
            cause.status_code,
            "Handoff label update failed.",
        )

        self.processor.process(
            durable_job(
                stage="delivering",
                delivery_attempts=5,
                action=PreparedAction.handoff("转人工", "knowledge_gap"),
            )
        )

        self.assertEqual(self.store.failures[0].error_code, "label_update_failed")
        self.assertEqual(self.store.retries, [])

    def test_retry_delays_are_fixed_and_bounded(self) -> None:
        self.assertEqual(
            [retry_delay_seconds(attempt) for attempt in range(1, 5)],
            [5, 30, 120, 600],
        )


if __name__ == "__main__":
    unittest.main()
