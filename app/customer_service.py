import logging
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Literal, Protocol, cast

from app.chatwoot_client import ChatwootAPIError
from app.chatwoot_events import IncomingMessage
from app.conversation_store import ConversationState
from app.dify_chat_client import DifyChatAPIError, DifyChatResponse
from app.handoff_policy import handoff_reason, parse_dify_answer
from app.operations_models import (
    NullOperationsRecorder,
    OperationsRecorder,
    Outcome,
    Reason,
)


logger = logging.getLogger(__name__)


HandlingStatus = Literal[
    "replied",
    "handed_off",
    "ignored_human_mode",
    "duplicate",
]


class ConversationStore(Protocol):
    def claim_message(self, message_id: int) -> bool: ...

    def complete_message(self, message_id: int) -> None: ...

    def release_message(self, message_id: int) -> None: ...

    def get_conversation(self, conversation_id: int) -> ConversationState | None: ...

    def save_conversation(
        self,
        conversation_id: int,
        *,
        dify_conversation_id: str | None,
        mode: Literal["ai", "human"],
    ) -> None: ...


class DifyChatProvider(Protocol):
    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse: ...


class ChatwootPublisher(Protocol):
    def send_outgoing_message(
        self,
        account_id: int,
        conversation_id: int,
        content: str,
    ) -> int: ...

    def add_label_preserving_existing(
        self,
        account_id: int,
        conversation_id: int,
        label: str,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class HandlingResult:
    status: HandlingStatus


class CustomerService:
    _DEFAULT_HANDOFF_TEXT = "正在为你转接人工客服，请稍候。"

    def __init__(
        self,
        *,
        store: ConversationStore,
        dify_client: DifyChatProvider,
        chatwoot_client: ChatwootPublisher,
        operations_recorder: OperationsRecorder | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._store = store
        self._dify = dify_client
        self._chatwoot = chatwoot_client
        self._operations = operations_recorder or NullOperationsRecorder()
        self._monotonic = monotonic

    def handle(self, message: IncomingMessage) -> HandlingResult:
        started_at = self._monotonic()
        if not self._store.claim_message(message.message_id):
            self._record_event(
                message,
                outcome="duplicate",
                reason=None,
                started_at=started_at,
            )
            return HandlingResult(status="duplicate")

        try:
            state = self._store.get_conversation(message.conversation_id)
            if state is not None and state.mode == "human":
                self._store.complete_message(message.message_id)
                self._record_event(
                    message,
                    outcome="ignored_human_mode",
                    reason=None,
                    started_at=started_at,
                )
                return HandlingResult(status="ignored_human_mode")

            rule_reason = handoff_reason(message.content)
            if rule_reason is not None:
                self._handoff(message, self._DEFAULT_HANDOFF_TEXT)
                self._store.complete_message(message.message_id)
                self._record_event(
                    message,
                    outcome="handed_off",
                    reason=cast(Reason, rule_reason),
                    started_at=started_at,
                )
                return HandlingResult(status="handed_off")

            try:
                response = self._dify.send_message(
                    query=message.content,
                    user=f"chatwoot:{message.sender_id}",
                    conversation_id=(state.dify_conversation_id if state else None),
                )
            except DifyChatAPIError:
                self._handoff(message, self._DEFAULT_HANDOFF_TEXT)
                self._store.complete_message(message.message_id)
                self._record_event(
                    message,
                    outcome="handed_off",
                    reason="dify_error",
                    started_at=started_at,
                )
                return HandlingResult(status="handed_off")

            parsed = parse_dify_answer(response.answer)
            if parsed.needs_human:
                is_knowledge_gap = response.answer.strip().upper().startswith(
                    "[HANDOFF]"
                )
                self._handoff(message, parsed.customer_text)
                self._store.complete_message(message.message_id)
                self._record_event(
                    message,
                    outcome="handed_off",
                    reason=("knowledge_gap" if is_knowledge_gap else "dify_error"),
                    started_at=started_at,
                )
                if is_knowledge_gap:
                    self._record_gap(message)
                return HandlingResult(status="handed_off")

            self._chatwoot.send_outgoing_message(
                message.account_id,
                message.conversation_id,
                parsed.customer_text,
            )
            self._store.save_conversation(
                message.conversation_id,
                dify_conversation_id=response.conversation_id,
                mode="ai",
            )
            self._store.complete_message(message.message_id)
            self._record_event(
                message,
                outcome="replied",
                reason=None,
                started_at=started_at,
            )
            return HandlingResult(status="replied")
        except Exception as exc:
            self._store.release_message(message.message_id)
            self._record_event(
                message,
                outcome="failed",
                reason=(
                    "chatwoot_error"
                    if isinstance(exc, ChatwootAPIError)
                    else "internal_error"
                ),
                started_at=started_at,
            )
            raise

    def _record_event(
        self,
        message: IncomingMessage,
        *,
        outcome: Outcome,
        reason: Reason | None,
        started_at: float,
    ) -> None:
        duration_ms = max(0, int((self._monotonic() - started_at) * 1000))
        try:
            self._operations.record_event(
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                outcome=outcome,
                reason=reason,
                duration_ms=duration_ms,
            )
        except Exception:
            logger.warning("Operations event recording failed.", exc_info=True)

    def _record_gap(self, message: IncomingMessage) -> None:
        try:
            self._operations.record_knowledge_gap(
                question=message.content,
                conversation_id=message.conversation_id,
            )
        except Exception:
            logger.warning(
                "Operations knowledge-gap recording failed.",
                exc_info=True,
            )

    def _handoff(self, message: IncomingMessage, customer_text: str) -> None:
        self._chatwoot.send_outgoing_message(
            message.account_id,
            message.conversation_id,
            customer_text,
        )
        try:
            self._chatwoot.add_label_preserving_existing(
                message.account_id,
                message.conversation_id,
                "ai_handoff",
            )
        except ChatwootAPIError:
            pass
        self._store.save_conversation(
            message.conversation_id,
            dify_conversation_id=None,
            mode="human",
        )
