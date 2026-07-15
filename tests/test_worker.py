import threading
import time
import unittest

from app.chatwoot_client import ChatwootAPIError
from app.job_models import DurableJob, PreparedAction
from app.settings import WorkerSettings
from app.worker import WorkerSupervisor, retry_failed_job


def worker_settings(*, concurrency: int = 2) -> WorkerSettings:
    return WorkerSettings(
        concurrency=concurrency,
        lease_seconds=60,
        reconcile_interval_seconds=60,
        generation_max_attempts=2,
        delivery_max_attempts=5,
    )


def job(job_id: int, conversation_id: int) -> DurableJob:
    return DurableJob(
        id=job_id,
        message_id=100 + job_id,
        account_id=1,
        conversation_id=conversation_id,
        sender_id=9,
        content="问题",
        labels=(),
        stage="delivering",
        resume_stage=None,
        action=PreparedAction.reply("答复", f"dify-{job_id}"),
        outgoing_source_id=f"ai-cs:{100 + job_id}:reply",
        generation_attempts=1,
        delivery_attempts=1,
        processing_duration_ms=100,
        available_at=1_000,
        locked_at=1_000,
        locked_by="worker",
        last_error_code=None,
    )


class FakeWorkerStore:
    def __init__(self, jobs: list[DurableJob] | None = None) -> None:
        self._jobs = list(jobs or [])
        self._lock = threading.Lock()
        self.claim_worker_ids: list[str] = []
        self.heartbeats: list[tuple[str, int]] = []
        self.recoveries: list[tuple[int, int]] = []
        self.events: list[str] = []
        self.stop_on_claim: threading.Event | None = None
        self.retry_result = False
        self.dependencies: list[dict[str, object]] = []

    def claim_next(
        self,
        worker_id: str,
        *,
        now: int,
        lease_seconds: int,
    ) -> DurableJob | None:
        with self._lock:
            self.claim_worker_ids.append(worker_id)
            self.events.append("claim")
            if self.stop_on_claim is not None:
                self.stop_on_claim.set()
            return self._jobs.pop(0) if self._jobs else None

    def recover_expired_leases(self, *, now: int, lease_seconds: int) -> int:
        with self._lock:
            self.recoveries.append((now, lease_seconds))
            self.events.append("recover")
        return 0

    def record_heartbeat(self, worker_id: str, *, now: int) -> None:
        with self._lock:
            self.heartbeats.append((worker_id, now))
            self.events.append("heartbeat")

    def retry_failed_message(self, message_id: int, *, now: int) -> bool:
        return self.retry_result

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


class FakeReconciler:
    def __init__(
        self,
        *,
        stop_event: threading.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.stop_event = stop_event
        self.error = error
        self.calls: list[int] = []

    def run_once(self, *, now: int) -> int:
        self.calls.append(now)
        if self.error is not None:
            raise self.error
        if len(self.calls) >= 2 and self.stop_event is not None:
            self.stop_event.set()
        return 0


class RecordingProcessor:
    def __init__(self, *, block_until: threading.Event | None = None) -> None:
        self.jobs: list[DurableJob] = []
        self._lock = threading.Lock()
        self.block_until = block_until
        self.two_started = threading.Event()
        self.one_started = threading.Event()

    def process(self, claimed_job: DurableJob) -> None:
        with self._lock:
            self.jobs.append(claimed_job)
            self.one_started.set()
            if len(self.jobs) >= 2:
                self.two_started.set()
        if self.block_until is not None:
            self.block_until.wait(timeout=2)


class WorkerSupervisorTests(unittest.TestCase):
    def test_default_settings_create_exactly_two_stable_worker_loops(self) -> None:
        stop_event = threading.Event()
        store = FakeWorkerStore()

        def idle_wait(seconds: float) -> None:
            if len(set(store.claim_worker_ids)) >= 2:
                stop_event.set()
            else:
                time.sleep(0.001)

        supervisor = WorkerSupervisor(
            store=store,
            processor=RecordingProcessor(),
            settings=worker_settings(),
            hostname="worker-host",
            process_id=123,
            idle_wait=idle_wait,
        )

        supervisor.run(stop_event)

        self.assertEqual(
            set(store.claim_worker_ids),
            {"worker-host:123:0", "worker-host:123:1"},
        )

    def test_different_conversations_process_concurrently(self) -> None:
        stop_event = threading.Event()
        release = threading.Event()
        store = FakeWorkerStore([job(1, 7), job(2, 8)])
        processor = RecordingProcessor(block_until=release)
        supervisor = WorkerSupervisor(
            store=store,
            processor=processor,
            settings=worker_settings(),
            hostname="worker-host",
            process_id=123,
            heartbeat_interval_seconds=0.01,
        )
        runner = threading.Thread(target=supervisor.run, args=(stop_event,))
        runner.start()
        try:
            self.assertTrue(processor.two_started.wait(timeout=1))
            self.assertEqual({item.conversation_id for item in processor.jobs}, {7, 8})
        finally:
            stop_event.set()
            release.set()
            runner.join(timeout=2)
        self.assertFalse(runner.is_alive())

    def test_heartbeat_continues_while_processor_is_blocked(self) -> None:
        stop_event = threading.Event()
        release = threading.Event()
        store = FakeWorkerStore([job(1, 7)])
        processor = RecordingProcessor(block_until=release)
        supervisor = WorkerSupervisor(
            store=store,
            processor=processor,
            settings=worker_settings(concurrency=1),
            heartbeat_interval_seconds=0.01,
        )
        runner = threading.Thread(target=supervisor.run, args=(stop_event,))
        runner.start()
        try:
            self.assertTrue(processor.one_started.wait(timeout=1))
            deadline = time.monotonic() + 1
            while len(store.heartbeats) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreaterEqual(len(store.heartbeats), 2)
        finally:
            stop_event.set()
            release.set()
            runner.join(timeout=2)
        self.assertFalse(runner.is_alive())

    def test_expired_leases_are_recovered_before_first_claim(self) -> None:
        stop_event = threading.Event()
        store = FakeWorkerStore()
        store.stop_on_claim = stop_event
        supervisor = WorkerSupervisor(
            store=store,
            processor=RecordingProcessor(),
            settings=worker_settings(concurrency=1),
            clock=lambda: 1_000,
        )

        supervisor.run(stop_event)

        self.assertLess(store.events.index("recover"), store.events.index("claim"))
        self.assertEqual(store.recoveries[0], (1_000, 60))

    def test_no_job_polling_uses_injected_wait(self) -> None:
        stop_event = threading.Event()
        waits: list[float] = []

        def idle_wait(seconds: float) -> None:
            waits.append(seconds)
            stop_event.set()

        supervisor = WorkerSupervisor(
            store=FakeWorkerStore(),
            processor=RecordingProcessor(),
            settings=worker_settings(concurrency=1),
            idle_wait=idle_wait,
        )

        supervisor.run(stop_event)

        self.assertEqual(waits, [0.25])

    def test_retry_failed_job_reports_store_result(self) -> None:
        store = FakeWorkerStore()
        self.assertFalse(retry_failed_job(store, 999, 1_000))
        store.retry_result = True
        self.assertTrue(retry_failed_job(store, 101, 1_001))

    def test_reconciliation_runs_at_startup_and_configured_interval(self) -> None:
        stop_event = threading.Event()
        store = FakeWorkerStore()
        reconciler = FakeReconciler(stop_event=stop_event)
        current = 999

        def clock() -> int:
            nonlocal current
            current += 1
            return current

        settings = worker_settings(concurrency=1)
        settings = WorkerSettings(
            concurrency=settings.concurrency,
            lease_seconds=settings.lease_seconds,
            reconcile_interval_seconds=1,
            generation_max_attempts=settings.generation_max_attempts,
            delivery_max_attempts=settings.delivery_max_attempts,
        )
        supervisor = WorkerSupervisor(
            store=store,
            processor=RecordingProcessor(),
            settings=settings,
            clock=clock,
            reconciler=reconciler,
            heartbeat_interval_seconds=0.01,
        )

        supervisor.run(stop_event)

        self.assertEqual(len(reconciler.calls), 2)
        self.assertEqual(reconciler.calls[0], 1_000)
        self.assertGreaterEqual(reconciler.calls[1], 1_001)

    def test_reconciliation_error_is_sanitized_and_does_not_stop_workers(self) -> None:
        stop_event = threading.Event()
        store = FakeWorkerStore()
        store.stop_on_claim = stop_event
        reconciler = FakeReconciler(
            error=ChatwootAPIError(503, "sensitive remote payload")
        )
        supervisor = WorkerSupervisor(
            store=store,
            processor=RecordingProcessor(),
            settings=worker_settings(concurrency=1),
            reconciler=reconciler,
            clock=lambda: 1_000,
        )

        with self.assertLogs("app.worker", level="WARNING") as logs:
            supervisor.run(stop_event)

        self.assertGreaterEqual(len(store.claim_worker_ids), 1)
        self.assertEqual(store.dependencies[-1]["ok"], False)
        self.assertEqual(store.dependencies[-1]["error_code"], "chatwoot_server_error")
        self.assertFalse(any("sensitive remote payload" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
