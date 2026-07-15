from dataclasses import dataclass
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
