import logging

from app.chatwoot_events import IncomingMessage
from app.job_models import EnqueueResult
from app.job_store import SQLiteJobStore
from app.operations_models import OperationsRecorder


class InboundQueue:
    def __init__(self, store: SQLiteJobStore, operations: OperationsRecorder) -> None:
        self._store = store
        self._operations = operations

    def enqueue(self, message: IncomingMessage) -> EnqueueResult:
        result = self._store.enqueue(message)
        if result.status == "duplicate":
            try:
                self._operations.record_event(
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    outcome="duplicate",
                    reason=None,
                    duration_ms=0,
                )
            except Exception:
                logging.getLogger(__name__).warning(
                    "Duplicate metric recording failed.",
                    exc_info=True,
                )
        return result
