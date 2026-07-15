from typing import Protocol

from app.chatwoot_client import ChatwootConversation
from app.chatwoot_events import IncomingMessage
from app.job_models import EnqueueResult


_OVERLAP_SECONDS = 300


class ReconciliationStore(Protocol):
    def get_reconciliation_cursor(self) -> int | None: ...

    def initialize_reconciliation_cursor(self, *, now: int) -> bool: ...

    def advance_reconciliation_cursor(
        self,
        *,
        expected: int,
        new_value: int,
    ) -> bool: ...


class IncomingQueue(Protocol):
    def enqueue(self, message: IncomingMessage) -> EnqueueResult: ...


class ChatwootReader(Protocol):
    def list_conversations_page(
        self,
        account_id: int,
        page: int,
    ) -> tuple[ChatwootConversation, ...]: ...

    def list_conversation_messages(
        self,
        account_id: int,
        conversation_id: int,
    ) -> tuple[dict[str, object], ...]: ...


class ChatwootReconciler:
    def __init__(
        self,
        *,
        account_id: int,
        store: ReconciliationStore,
        queue: IncomingQueue,
        chatwoot: ChatwootReader,
    ) -> None:
        if (
            isinstance(account_id, bool)
            or not isinstance(account_id, int)
            or account_id <= 0
        ):
            raise ValueError("Chatwoot account ID must be a positive integer.")
        self._account_id = account_id
        self._store = store
        self._queue = queue
        self._chatwoot = chatwoot

    def run_once(self, *, now: int) -> int:
        cursor = self._store.get_reconciliation_cursor()
        if cursor is None:
            self._store.initialize_reconciliation_cursor(now=now)
            return 0

        cutoff = cursor - _OVERLAP_SECONDS
        queued_count = 0
        page = 1
        while True:
            conversations = self._chatwoot.list_conversations_page(
                self._account_id,
                page,
            )
            if not conversations:
                break
            if all(item.last_activity_at < cutoff for item in conversations):
                break

            for conversation in conversations:
                if conversation.last_activity_at < cutoff:
                    continue
                raw_messages = self._chatwoot.list_conversation_messages(
                    self._account_id,
                    conversation.id,
                )
                for raw_message in raw_messages:
                    message = self._parse_message(
                        conversation,
                        raw_message,
                        cutoff=cutoff,
                    )
                    if message is None:
                        continue
                    result = self._queue.enqueue(message)
                    if result.status == "queued":
                        queued_count += 1
            page += 1

        if not self._store.advance_reconciliation_cursor(
            expected=cursor,
            new_value=now,
        ):
            raise RuntimeError("Reconciliation cursor changed during scan.")
        return queued_count

    def _parse_message(
        self,
        conversation: ChatwootConversation,
        payload: dict[str, object],
        *,
        cutoff: int,
    ) -> IncomingMessage | None:
        message_type = payload.get("message_type")
        incoming = message_type == "incoming" or (
            isinstance(message_type, int)
            and not isinstance(message_type, bool)
            and message_type == 0
        )
        if not incoming or payload.get("private") is not False:
            return None
        if payload.get("content_type") not in {None, "text"}:
            return None

        message_id = payload.get("id")
        created_at = payload.get("created_at")
        content = payload.get("content")
        sender = payload.get("sender")
        sender_id = sender.get("id") if isinstance(sender, dict) else None
        if (
            not _is_positive_int(message_id)
            or not _is_positive_int(sender_id)
            or not isinstance(created_at, int)
            or isinstance(created_at, bool)
            or created_at < cutoff
            or not isinstance(content, str)
            or not content.strip()
        ):
            return None

        return IncomingMessage(
            message_id=message_id,
            account_id=self._account_id,
            conversation_id=conversation.id,
            sender_id=sender_id,
            content=content.strip(),
            labels=conversation.labels,
        )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
