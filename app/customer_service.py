import logging
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Literal, Protocol, cast

from app.chatwoot_client import ChatwootAPIError
from app.chatwoot_events import IncomingMessage
from app.conversation_store import ConversationState
from app.dify_chat_client import DifyChatAPIError, DifyChatResponse
from app.handoff_policy import (
    handoff_reason,
    has_handoff_sentinel,
    parse_dify_answer,
)
from app.job_models import PreparedAction
from app.operations_models import (
    NullOperationsRecorder,
    OperationsRecorder,
    Outcome,
    Reason,
)


logger = logging.getLogger(__name__)


_HUMAN_WAIT_TEXT = (
    "人工客服暂时未能及时回复，我们已提醒工作人员，请稍候。"
    "如涉及支付、账户或投诉问题，请不要发送密码、验证码等敏感信息。"
)


class HandoffLabelError(ChatwootAPIError):
    pass


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

    def save_handoff(
        self,
        message: IncomingMessage,
        reason: Reason,
        *,
        now: int | None = None,
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
    def find_outgoing_message_by_source_id(
        self,
        account_id: int,
        conversation_id: int,
        source_id: str,
    ) -> int | None: ...

    def send_outgoing_message(
        self,
        account_id: int,
        conversation_id: int,
        content: str,
        *,
        source_id: str | None = None,
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


@dataclass(frozen=True)
class HumanFallbackPreparation:
    action: PreparedAction
    job_kind: Literal["human_timeout_reply", "human_wait_notice"]
    dify_used: bool


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
        handoff_message: str = _DEFAULT_HANDOFF_TEXT,
    ) -> None:
        if not isinstance(handoff_message, str):
            raise ValueError("handoff_message must contain 1-500 characters.")
        normalized_handoff_message = handoff_message.strip()
        if not normalized_handoff_message or len(normalized_handoff_message) > 500:
            raise ValueError("handoff_message must contain 1-500 characters.")
        self._store = store
        self._dify = dify_client
        self._chatwoot = chatwoot_client
        self._operations = operations_recorder or NullOperationsRecorder()
        self._monotonic = monotonic
        self._handoff_message = normalized_handoff_message

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
            try:
                action = self.prepare(message)
            except DifyChatAPIError:
                action = self.dify_error_action()

            self.deliver(
                message,
                action,
                source_id=f"ai-cs:{message.message_id}:reply",
            )
            self._store.complete_message(message.message_id)
            self.record_result(
                message,
                action,
                duration_ms=self._elapsed_ms(started_at),
            )
            return HandlingResult(status=cast(HandlingStatus, action.outcome))
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

    def prepare(self, message: IncomingMessage) -> PreparedAction:
        state = self._store.get_conversation(message.conversation_id)
        if state is not None and state.mode == "human":
            return PreparedAction.ignore_human_mode()

        rule_reason = handoff_reason(message.content)
        if rule_reason is not None:
            return PreparedAction.handoff(
                self._handoff_message,
                cast(Reason, rule_reason),
            )

        response = self._dify.send_message(
            query=message.content,
            user=f"chatwoot:{message.sender_id}",
            conversation_id=(state.dify_conversation_id if state else None),
        )
        parsed = parse_dify_answer(response.answer)
        if parsed.needs_human:
            reason: Reason = (
                "knowledge_gap"
                if has_handoff_sentinel(response.answer)
                else "dify_error"
            )
            return PreparedAction.handoff(parsed.customer_text, reason)
        return PreparedAction.reply(parsed.customer_text, response.conversation_id)

    def dify_error_action(self) -> PreparedAction:
        return PreparedAction.handoff(self._handoff_message, "dify_error")

    def human_wait_text(self) -> str:
        return _HUMAN_WAIT_TEXT

    def prepare_human_fallback(
        self,
        message: IncomingMessage,
    ) -> HumanFallbackPreparation:
        if handoff_reason(message.content) is not None:
            return HumanFallbackPreparation(
                PreparedAction.customer_message(_HUMAN_WAIT_TEXT),
                "human_wait_notice",
                False,
            )

        response = self._dify.send_message(
            query=message.content,
            user=f"chatwoot:{message.sender_id}",
            conversation_id=None,
        )
        parsed = parse_dify_answer(response.answer)
        if parsed.needs_human:
            return HumanFallbackPreparation(
                PreparedAction.customer_message(
                    _HUMAN_WAIT_TEXT,
                    record_knowledge_gap=(
                        has_handoff_sentinel(response.answer)
                    ),
                ),
                "human_wait_notice",
                True,
            )

        return HumanFallbackPreparation(
            PreparedAction.customer_message(
                f"AI 临时协助：{parsed.customer_text}\n\n"
                "人工客服仍在接管本会话，如需进一步处理请稍候。"
            ),
            "human_timeout_reply",
            True,
        )

    def deliver(
        self,
        message: IncomingMessage,
        action: PreparedAction,
        *,
        source_id: str,
    ) -> None:
        if action.decision == "ignore":
            return
        if action.customer_text is None:
            raise ValueError("Customer-visible action text is required.")

        existing_message_id = self._chatwoot.find_outgoing_message_by_source_id(
            message.account_id,
            message.conversation_id,
            source_id,
        )
        if existing_message_id is None:
            self._chatwoot.send_outgoing_message(
                message.account_id,
                message.conversation_id,
                action.customer_text,
                source_id=source_id,
            )

        if action.decision == "handoff":
            try:
                self._chatwoot.add_label_preserving_existing(
                    message.account_id,
                    message.conversation_id,
                    "ai_handoff",
                )
            except ChatwootAPIError as exc:
                raise HandoffLabelError(
                    exc.status_code,
                    "Chatwoot handoff label update failed.",
                ) from exc
            self._store.save_handoff(
                message,
                cast(Reason, action.reason),
                now=int(time.time()),
            )
            return

        self._store.save_conversation(
            message.conversation_id,
            dify_conversation_id=action.dify_conversation_id,
            mode="ai",
        )

    def deliver_human_fallback(
        self,
        message: IncomingMessage,
        action: PreparedAction,
        *,
        source_id: str,
    ) -> None:
        if action.customer_text is None:
            raise ValueError("Human fallback text is required.")
        existing_message_id = self._chatwoot.find_outgoing_message_by_source_id(
            message.account_id,
            message.conversation_id,
            source_id,
        )
        if existing_message_id is None:
            self._chatwoot.send_outgoing_message(
                message.account_id,
                message.conversation_id,
                action.customer_text,
                source_id=source_id,
            )

    def record_result(
        self,
        message: IncomingMessage,
        action: PreparedAction,
        duration_ms: int,
    ) -> None:
        try:
            self._operations.record_event(
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                outcome=action.outcome,
                reason=action.reason,
                duration_ms=max(0, duration_ms),
            )
        except Exception:
            logger.warning("Operations event recording failed.", exc_info=True)
        if action.record_knowledge_gap:
            self._record_gap(message)

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

    def _elapsed_ms(self, started_at: float) -> int:
        return max(0, int((self._monotonic() - started_at) * 1000))

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
