from dataclasses import dataclass
from typing import Literal

from app.operations_models import Outcome, Reason


JobStage = Literal[
    "queued",
    "generating",
    "human_wait",
    "ready_to_deliver",
    "delivering",
    "retry_wait",
    "completed",
    "failed",
]
ResumeStage = Literal["generating", "delivering"]
ClaimedFromStage = Literal[
    "queued",
    "human_wait",
    "ready_to_deliver",
    "retry_wait",
]
JobKind = Literal[
    "standard",
    "human_pending",
    "human_timeout_reply",
    "human_wait_notice",
]
Decision = Literal["reply", "handoff", "ignore"]


@dataclass(frozen=True)
class PreparedAction:
    decision: Decision
    outcome: Outcome
    customer_text: str | None
    dify_conversation_id: str | None
    reason: Reason | None
    record_knowledge_gap: bool

    @classmethod
    def reply(cls, text: str, conversation_id: str) -> "PreparedAction":
        return cls("reply", "replied", text.strip(), conversation_id.strip(), None, False)

    @classmethod
    def handoff(cls, text: str, reason: Reason) -> "PreparedAction":
        return cls("handoff", "handed_off", text.strip(), None, reason, reason == "knowledge_gap")

    @classmethod
    def ignore_human_mode(cls) -> "PreparedAction":
        return cls("ignore", "ignored_human_mode", None, None, None, False)

    @classmethod
    def customer_message(
        cls,
        text: str,
        *,
        record_knowledge_gap: bool = False,
    ) -> "PreparedAction":
        normalized = text.strip()
        if not normalized:
            raise ValueError("Customer-visible action text is required.")
        return cls(
            "reply",
            "ignored_human_mode",
            normalized,
            None,
            None,
            record_knowledge_gap,
        )


@dataclass(frozen=True)
class EnqueueResult:
    status: Literal["queued", "duplicate"]
    job_id: int | None


@dataclass(frozen=True)
class DurableJob:
    id: int
    message_id: int
    account_id: int
    conversation_id: int
    sender_id: int
    content: str
    labels: tuple[str, ...]
    stage: JobStage
    resume_stage: ResumeStage | None
    action: PreparedAction | None
    outgoing_source_id: str
    generation_attempts: int
    delivery_attempts: int
    processing_duration_ms: int
    available_at: int
    locked_at: int | None
    locked_by: str | None
    last_error_code: str | None
    claimed_from_stage: ClaimedFromStage | None = None
    message_created_at: int = 0
    received_at: int = 0
    job_kind: JobKind = "standard"


@dataclass(frozen=True)
class QueueMetrics:
    queued: int
    processing: int
    retry_wait: int
    failed: int
    oldest_wait_seconds: int
    last_worker_heartbeat: int | None
