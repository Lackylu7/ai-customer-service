import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from app.chatwoot_client import ChatwootAPIError
from app.job_store import (
    HumanConversationState,
    ResumePrecheckStatus,
    ResumeStoreStatus,
)


SlaStatus = Literal["normal", "due_soon", "overdue"]


@dataclass(frozen=True)
class HumanConversation:
    conversation_id: int
    handoff_reason: str | None
    human_since: int | None
    last_customer_message_at: int | None
    last_human_reply_at: int | None
    last_fallback_at: int | None
    waiting_seconds: int
    sla_status: SlaStatus
    has_blocking_job: bool
    chatwoot_url: str


@dataclass(frozen=True)
class ResumeResult:
    status: Literal["resumed", "already_ai"]


class HandoffNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Human conversation was not found.")


class HandoffConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Human conversation has a blocking job.")


class HandoffRemoteError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class HandoffStorageError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Human handoff state is temporarily unavailable.")


class HandoffStore(Protocol):
    def list_human_conversation_states(
        self,
    ) -> tuple[HumanConversationState, ...]: ...

    def get_resume_precheck(
        self,
        conversation_id: int,
    ) -> ResumePrecheckStatus: ...

    def resume_ai_transaction(
        self,
        conversation_id: int,
        *,
        cutoff_message_id: int,
        now: int,
    ) -> ResumeStoreStatus: ...

    def record_control_failure(
        self,
        conversation_id: int,
        action: Literal["ai_resumed"],
        error_code: str,
        *,
        now: int,
    ) -> None: ...


class HandoffChatwoot(Protocol):
    def latest_message_id(self, account_id: int, conversation_id: int) -> int: ...

    def remove_label_preserving_existing(
        self,
        account_id: int,
        conversation_id: int,
        label: str,
    ) -> tuple[str, ...]: ...


def chatwoot_control_error_code(error: ChatwootAPIError) -> str:
    if error.status_code in {401, 403}:
        return "chatwoot_auth_error"
    if error.status_code == 429:
        return "chatwoot_rate_limited"
    if error.status_code is not None and error.status_code >= 500:
        return "chatwoot_server_error"
    return (
        "chatwoot_timeout"
        if error.status_code is None
        else "chatwoot_request_error"
    )


class HandoffControlService:
    def __init__(
        self,
        *,
        store: HandoffStore,
        chatwoot: HandoffChatwoot,
        account_id: int,
        base_url: str,
        human_sla_seconds: int,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            raise ValueError("Chatwoot account ID must be a positive integer.")
        normalized_base_url = base_url.strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("Chatwoot base URL is required.")
        if human_sla_seconds <= 0:
            raise ValueError("Human SLA must be positive.")
        self._store = store
        self._chatwoot = chatwoot
        self._account_id = account_id
        self._base_url = normalized_base_url
        self._human_sla_seconds = human_sla_seconds
        self._clock = clock or (lambda: int(time.time()))

    def list_human_conversations(self) -> tuple[HumanConversation, ...]:
        try:
            states = self._store.list_human_conversation_states()
        except sqlite3.Error as exc:
            raise HandoffStorageError() from exc
        now = self._clock()
        items: list[HumanConversation] = []
        for state in states:
            human_replied = _event_not_before_customer(state)
            waiting_since = (
                state.last_customer_message_at
                if state.last_customer_message_at is not None
                else state.human_since
            )
            waiting_seconds = (
                0
                if human_replied or waiting_since is None
                else max(0, now - waiting_since)
            )
            if waiting_seconds >= self._human_sla_seconds:
                sla_status: SlaStatus = "overdue"
            elif waiting_seconds * 5 >= self._human_sla_seconds * 4:
                sla_status = "due_soon"
            else:
                sla_status = "normal"
            items.append(
                HumanConversation(
                    conversation_id=state.conversation_id,
                    handoff_reason=state.handoff_reason,
                    human_since=state.human_since,
                    last_customer_message_at=state.last_customer_message_at,
                    last_human_reply_at=state.last_human_reply_at,
                    last_fallback_at=state.last_fallback_at,
                    waiting_seconds=waiting_seconds,
                    sla_status=sla_status,
                    has_blocking_job=state.has_blocking_job,
                    chatwoot_url=(
                        f"{self._base_url}/app/accounts/{self._account_id}/"
                        f"conversations/{state.conversation_id}"
                    ),
                )
            )
        return tuple(items)

    def resume_ai(self, conversation_id: int) -> ResumeResult:
        if (
            isinstance(conversation_id, bool)
            or not isinstance(conversation_id, int)
            or conversation_id <= 0
        ):
            raise ValueError("Conversation ID must be a positive integer.")
        try:
            precheck = self._store.get_resume_precheck(conversation_id)
        except sqlite3.Error as exc:
            raise HandoffStorageError() from exc

        if precheck == "not_found":
            raise HandoffNotFoundError()
        if precheck == "already_ai":
            return ResumeResult("already_ai")
        if precheck == "conflict":
            raise HandoffConflictError()
        if precheck != "ready":
            raise HandoffStorageError()

        try:
            cutoff_message_id = self._chatwoot.latest_message_id(
                self._account_id,
                conversation_id,
            )
            self._chatwoot.remove_label_preserving_existing(
                self._account_id,
                conversation_id,
                "ai_handoff",
            )
        except ChatwootAPIError as exc:
            error_code = chatwoot_control_error_code(exc)
            try:
                self._store.record_control_failure(
                    conversation_id,
                    "ai_resumed",
                    error_code,
                    now=self._clock(),
                )
            except sqlite3.Error as storage_exc:
                raise HandoffStorageError() from storage_exc
            raise HandoffRemoteError(error_code) from exc

        try:
            status = self._store.resume_ai_transaction(
                conversation_id,
                cutoff_message_id=cutoff_message_id,
                now=self._clock(),
            )
        except sqlite3.Error as exc:
            raise HandoffStorageError() from exc

        if status == "conflict":
            raise HandoffConflictError()
        if status == "not_found":
            raise HandoffNotFoundError()
        if status in {"resumed", "already_ai"}:
            return ResumeResult(status)
        raise HandoffStorageError()


def _event_not_before_customer(state: HumanConversationState) -> bool:
    if (
        state.last_customer_message_at is None
        or state.last_customer_message_id is None
        or state.last_human_reply_at is None
        or state.last_human_reply_message_id is None
    ):
        return False
    return (
        state.last_human_reply_at,
        state.last_human_reply_message_id,
    ) >= (
        state.last_customer_message_at,
        state.last_customer_message_id,
    )
