import time
from collections.abc import Callable

from app.chatwoot_client import ChatwootAPIError
from app.chatwoot_events import IncomingMessage
from app.customer_service import CustomerService, HandoffLabelError
from app.dify_chat_client import DifyChatAPIError
from app.job_models import DurableJob
from app.job_store import SQLiteJobStore
from app.settings import WorkerSettings


_RETRY_DELAYS_SECONDS = (5, 30, 120, 600)


def retry_delay_seconds(attempt: int) -> int:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("Retry attempt must be a positive integer.")
    return _RETRY_DELAYS_SECONDS[min(attempt, len(_RETRY_DELAYS_SECONDS)) - 1]


class JobProcessor:
    def __init__(
        self,
        *,
        service: CustomerService,
        store: SQLiteJobStore,
        settings: WorkerSettings,
        clock: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._service = service
        self._store = store
        self._settings = settings
        self._clock = clock or (lambda: int(time.time()))
        self._monotonic = monotonic

    def process(self, job: DurableJob) -> None:
        if job.stage == "generating":
            self._process_generation(job)
            return
        if job.stage == "delivering":
            self._process_delivery(job)
            return
        raise ValueError(f"Job {job.id} is not in a processing stage.")

    def _process_generation(self, job: DurableJob) -> None:
        message = _incoming_message(job)
        started_at = self._monotonic()
        try:
            action = self._service.prepare(message)
        except DifyChatAPIError as exc:
            duration_ms = _duration_ms(started_at, self._monotonic())
            now = self._clock()
            error_code = _dify_error_code(exc)
            self._store.record_dependency_status(
                "dify",
                ok=False,
                error_code=error_code,
                checked_at=now,
            )
            if exc.status_code in {401, 403} or (
                job.generation_attempts >= self._settings.generation_max_attempts
            ):
                self._store.save_prepared(
                    job.id,
                    self._service.dify_error_action(),
                    processing_duration_ms=duration_ms,
                    now=now,
                )
                return
            self._store.schedule_retry(
                job.id,
                resume_stage="generating",
                error_code=error_code,
                available_at=now + retry_delay_seconds(job.generation_attempts),
                now=now,
            )
            return

        duration_ms = _duration_ms(started_at, self._monotonic())
        now = self._clock()
        self._store.save_prepared(
            job.id,
            action,
            processing_duration_ms=duration_ms,
            now=now,
        )
        self._store.record_dependency_status(
            "dify",
            ok=True,
            error_code=None,
            checked_at=now,
        )

    def _process_delivery(self, job: DurableJob) -> None:
        if job.action is None:
            raise ValueError(f"Job {job.id} has no prepared action.")
        message = _incoming_message(job)
        try:
            self._service.deliver(
                message,
                job.action,
                source_id=job.outgoing_source_id,
            )
        except HandoffLabelError as exc:
            self._handle_delivery_error(job, exc, "label_update_failed")
            return
        except ChatwootAPIError as exc:
            self._handle_delivery_error(job, exc, _chatwoot_error_code(exc))
            return

        self._service.record_result(
            message,
            job.action,
            duration_ms=job.processing_duration_ms,
        )
        now = self._clock()
        self._store.complete(job.id, now=now)
        self._store.record_dependency_status(
            "chatwoot",
            ok=True,
            error_code=None,
            checked_at=now,
        )

    def _handle_delivery_error(
        self,
        job: DurableJob,
        error: ChatwootAPIError,
        error_code: str,
    ) -> None:
        now = self._clock()
        self._store.record_dependency_status(
            "chatwoot",
            ok=False,
            error_code=error_code,
            checked_at=now,
        )
        if error.status_code in {401, 403} or (
            job.delivery_attempts >= self._settings.delivery_max_attempts
        ):
            self._store.fail(
                job.id,
                resume_stage="delivering",
                error_code=error_code,
                now=now,
            )
            return
        self._store.schedule_retry(
            job.id,
            resume_stage="delivering",
            error_code=error_code,
            available_at=now + retry_delay_seconds(job.delivery_attempts),
            now=now,
        )


def _incoming_message(job: DurableJob) -> IncomingMessage:
    return IncomingMessage(
        message_id=job.message_id,
        account_id=job.account_id,
        conversation_id=job.conversation_id,
        sender_id=job.sender_id,
        content=job.content,
        labels=job.labels,
    )


def _duration_ms(started_at: float, finished_at: float) -> int:
    return max(0, int((finished_at - started_at) * 1000))


def _dify_error_code(error: DifyChatAPIError) -> str:
    if error.status_code == 429:
        return "dify_rate_limited"
    if error.status_code in {401, 403}:
        return "dify_auth_error"
    if error.status_code is not None and error.status_code >= 500:
        return "dify_server_error"
    return "dify_timeout" if error.status_code is None else "dify_request_error"


def _chatwoot_error_code(error: ChatwootAPIError) -> str:
    if error.status_code in {401, 403}:
        return "chatwoot_auth_error"
    if error.status_code == 429:
        return "chatwoot_rate_limited"
    if error.status_code is not None and error.status_code >= 500:
        return "chatwoot_server_error"
    return "chatwoot_timeout" if error.status_code is None else "chatwoot_request_error"
