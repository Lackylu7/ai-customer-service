from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol


Outcome = Literal[
    "replied",
    "handed_off",
    "ignored_human_mode",
    "duplicate",
    "failed",
]
Reason = Literal[
    "explicit_human_request",
    "complaint_or_legal",
    "payment_or_account_security",
    "knowledge_gap",
    "dify_error",
    "chatwoot_error",
    "internal_error",
]
GapStatus = Literal["pending", "resolved", "ignored"]


@dataclass(frozen=True)
class ServiceEvent:
    id: int
    message_id: int
    conversation_id: int
    outcome: Outcome
    reason: Reason | None
    duration_ms: int
    created_at: int


@dataclass(frozen=True)
class KnowledgeGap:
    id: int
    fingerprint: str
    sample_question: str
    occurrence_count: int
    first_seen_at: int
    last_seen_at: int
    last_conversation_id: int
    status: GapStatus
    note: str
    updated_at: int


class OperationsRecorder(Protocol):
    def record_event(
        self,
        *,
        message_id: int,
        conversation_id: int,
        outcome: Outcome,
        reason: Reason | None,
        duration_ms: int,
        created_at: int | None = None,
    ) -> None: ...

    def record_knowledge_gap(
        self,
        *,
        question: str,
        conversation_id: int,
        created_at: int | None = None,
    ) -> None: ...


class RecoveringOperationsRecorder:
    def __init__(self, factory: Callable[[], OperationsRecorder]) -> None:
        self._factory = factory
        self._delegate: OperationsRecorder | None = None
        self._lock = Lock()

    def record_event(
        self,
        *,
        message_id: int,
        conversation_id: int,
        outcome: Outcome,
        reason: Reason | None,
        duration_ms: int,
        created_at: int | None = None,
    ) -> None:
        self._recorder().record_event(
            message_id=message_id,
            conversation_id=conversation_id,
            outcome=outcome,
            reason=reason,
            duration_ms=duration_ms,
            created_at=created_at,
        )

    def record_knowledge_gap(
        self,
        *,
        question: str,
        conversation_id: int,
        created_at: int | None = None,
    ) -> None:
        self._recorder().record_knowledge_gap(
            question=question,
            conversation_id=conversation_id,
            created_at=created_at,
        )

    def _recorder(self) -> OperationsRecorder:
        if self._delegate is not None:
            return self._delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._factory()
            return self._delegate


class NullOperationsRecorder:
    def record_event(
        self,
        *,
        message_id: int,
        conversation_id: int,
        outcome: Outcome,
        reason: Reason | None,
        duration_ms: int,
        created_at: int | None = None,
    ) -> None:
        return None

    def record_knowledge_gap(
        self,
        *,
        question: str,
        conversation_id: int,
        created_at: int | None = None,
    ) -> None:
        return None
