import argparse
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.chatwoot_client import ChatwootAPIError, ChatwootClient
from app.chatwoot_reconciliation import ChatwootReconciler
from app.conversation_store import SQLiteConversationStore
from app.customer_service import CustomerService
from app.dify_chat_client import DifyChatClient
from app.inbound_queue import InboundQueue
from app.job_processor import JobProcessor
from app.job_store import SQLiteJobStore
from app.operations_models import RecoveringOperationsRecorder
from app.operations_store import OperationsStore
from app.settings import Settings, WorkerSettings


logger = logging.getLogger(__name__)


class WorkerSupervisor:
    def __init__(
        self,
        *,
        store: SQLiteJobStore,
        processor: JobProcessor,
        settings: WorkerSettings,
        clock: Callable[[], int] | None = None,
        hostname: str | None = None,
        process_id: int | None = None,
        idle_wait: Callable[[float], None] | None = None,
        heartbeat_interval_seconds: float = 15,
        reconciler: ChatwootReconciler | None = None,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("Heartbeat interval must be positive.")
        self._store = store
        self._processor = processor
        self._settings = settings
        self._clock = clock or (lambda: int(time.time()))
        self._hostname = hostname or socket.gethostname()
        self._process_id = os.getpid() if process_id is None else process_id
        self._idle_wait = idle_wait
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._reconciler = reconciler

    def run(self, stop_event: threading.Event) -> None:
        supervisor_id = f"{self._hostname}:{self._process_id}:supervisor"
        now = self._clock()
        self._maintain(supervisor_id, now=now)
        self._run_reconciliation(now=now)
        next_reconciliation_at = now + self._settings.reconcile_interval_seconds
        supervisor_interval_seconds = min(
            self._heartbeat_interval_seconds,
            self._settings.reconcile_interval_seconds,
        )
        threads = [
            threading.Thread(
                target=self._worker_loop,
                args=(slot, stop_event),
                name=f"customer-service-worker-{slot}",
                daemon=True,
            )
            for slot in range(self._settings.concurrency)
        ]
        for thread in threads:
            thread.start()

        try:
            while not stop_event.wait(supervisor_interval_seconds):
                now = self._clock()
                self._maintain(supervisor_id, now=now)
                if now >= next_reconciliation_at:
                    self._run_reconciliation(now=now)
                    next_reconciliation_at = (
                        now + self._settings.reconcile_interval_seconds
                    )
        finally:
            stop_event.set()
            deadline = time.monotonic() + self._settings.lease_seconds
            for thread in threads:
                remaining = max(0.0, deadline - time.monotonic())
                thread.join(timeout=remaining)

    def _maintain(self, supervisor_id: str, *, now: int) -> None:
        self._store.recover_expired_leases(
            now=now,
            lease_seconds=self._settings.lease_seconds,
        )
        self._store.record_heartbeat(supervisor_id, now=now)

    def _run_reconciliation(self, *, now: int) -> None:
        if self._reconciler is None:
            return
        try:
            self._reconciler.run_once(now=now)
        except Exception as exc:
            error_code = _reconciliation_error_code(exc)
            try:
                self._store.record_dependency_status(
                    "chatwoot",
                    ok=False,
                    error_code=error_code,
                    checked_at=now,
                )
            except Exception:
                logger.warning("Chatwoot dependency status recording failed.")
            logger.warning("Chatwoot reconciliation failed.")
            return
        try:
            self._store.record_dependency_status(
                "chatwoot",
                ok=True,
                error_code=None,
                checked_at=now,
            )
        except Exception:
            logger.warning("Chatwoot dependency status recording failed.")

    def _worker_loop(self, slot: int, stop_event: threading.Event) -> None:
        worker_id = f"{self._hostname}:{self._process_id}:{slot}"
        while not stop_event.is_set():
            try:
                claimed_job = self._store.claim_next(
                    worker_id,
                    now=self._clock(),
                    lease_seconds=self._settings.lease_seconds,
                )
                if claimed_job is None:
                    if self._idle_wait is None:
                        stop_event.wait(0.25)
                    else:
                        self._idle_wait(0.25)
                    continue
                self._processor.process(claimed_job)
            except Exception:
                logger.exception(
                    "Customer-service job processing failed.",
                    extra={"worker_id": worker_id},
                )


def retry_failed_job(store: SQLiteJobStore, message_id: int, now: int) -> bool:
    return store.retry_failed_message(message_id, now=now)


def build_supervisor() -> WorkerSupervisor:
    settings = Settings.from_env()
    worker_settings = WorkerSettings.from_env()
    job_store = SQLiteJobStore(settings.customer_service_db_path)
    operations = RecoveringOperationsRecorder(
        lambda: OperationsStore(settings.customer_service_db_path)
    )
    chatwoot_client = ChatwootClient(
        base_url=settings.chatwoot_base_url,
        api_token=settings.chatwoot_api_token,
        timeout=15,
    )
    service = CustomerService(
        store=SQLiteConversationStore(settings.customer_service_db_path),
        dify_client=DifyChatClient(
            base_url=settings.dify_base_url,
            api_key=settings.dify_app_api_key,
            timeout=60,
        ),
        chatwoot_client=chatwoot_client,
        operations_recorder=operations,
        handoff_message=settings.enterprise_config.assistant.handoff_message,
    )
    processor = JobProcessor(
        service=service,
        store=job_store,
        settings=worker_settings,
    )
    return WorkerSupervisor(
        store=job_store,
        processor=processor,
        settings=worker_settings,
        reconciler=ChatwootReconciler(
            account_id=settings.chatwoot_account_id,
            store=job_store,
            queue=InboundQueue(job_store, operations),
            chatwoot=chatwoot_client,
        ),
    )


def _reconciliation_error_code(error: Exception) -> str:
    if not isinstance(error, ChatwootAPIError):
        return "chatwoot_reconciliation_error"
    if error.status_code in {401, 403}:
        return "chatwoot_auth_error"
    if error.status_code == 429:
        return "chatwoot_rate_limited"
    if error.status_code is not None and error.status_code >= 500:
        return "chatwoot_server_error"
    return "chatwoot_timeout" if error.status_code is None else "chatwoot_request_error"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the durable customer-service worker.")
    parser.add_argument("--retry-message-id", type=int)
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    if arguments.retry_message_id is not None:
        if arguments.retry_message_id <= 0:
            parser.error("--retry-message-id must be positive")
        database_path = Path(
            os.getenv(
                "CUSTOMER_SERVICE_DB_PATH",
                ".runtime/customer-service/state.db",
            )
        )
        store = SQLiteJobStore(database_path)
        if not retry_failed_job(
            store,
            arguments.retry_message_id,
            int(time.time()),
        ):
            raise SystemExit("The failed customer-service job was not found.")
        print("PASS: Failed customer-service job was queued for retry.")
        return 0

    stop_event = threading.Event()

    def request_stop(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    build_supervisor().run(stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
