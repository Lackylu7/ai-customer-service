import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Literal, Protocol

from app.enterprise_assistant import EnterpriseAnswer, EnterpriseAssistant
from app.enterprise_policy import (
    BusinessDomain,
    PolicyAction,
    PolicyReason,
)
from app.operations_privacy import redact_question


DingTalkChannel = Literal["group", "direct"]
InteractionFeedback = Literal["resolved", "unresolved", "human_requested"]
InteractionStatus = Literal[
    "awaiting_feedback",
    "awaiting_clarification",
    "needs_human",
    "resolved",
    "closed",
]
KnowledgeReviewStatus = Literal["not_reviewed", "approved", "not_reusable"]
CollaborationStatus = Literal["active", "resolved"]


@dataclass(frozen=True)
class DingTalkInboundMessage:
    message_id: str
    sender_id: str
    conversation_id: str
    text: str
    channel: DingTalkChannel
    sender_is_bot: bool = False


@dataclass(frozen=True)
class DingTalkInteraction:
    interaction_id: int
    ticket_code: str
    message_id: str
    sender_id: str
    conversation_id: str
    channel: DingTalkChannel
    question: str
    answer: str
    domain: BusinessDomain
    action: PolicyAction
    reason: PolicyReason | None
    feedback: InteractionFeedback | None
    status: InteractionStatus
    owner: str | None
    owner_id: str | None
    final_answer: str | None
    reviewed_by: str | None
    knowledge_status: KnowledgeReviewStatus
    collaboration_group_id: str | None
    collaboration_group_name: str | None
    collaboration_status: CollaborationStatus | None
    created_at: int
    updated_at: int


class DingTalkState(Protocol):
    def is_processed(self, message_id: str) -> bool: ...

    def mark_processed(self, message_id: str) -> None: ...

    def get_conversation_id(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> str | None: ...

    def set_conversation_id(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        backend_conversation_id: str,
    ) -> None: ...

    def get_domain(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> BusinessDomain | None: ...

    def set_domain(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        domain: BusinessDomain,
    ) -> None: ...

    def record_interaction(
        self,
        *,
        message: DingTalkInboundMessage,
        answer: EnterpriseAnswer,
    ) -> DingTalkInteraction: ...

    def record_feedback(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        feedback: InteractionFeedback,
    ) -> DingTalkInteraction | None: ...

    def list_interactions(
        self,
        *,
        status: InteractionStatus | None = None,
    ) -> tuple[DingTalkInteraction, ...]: ...

    def get_interaction(self, ticket_code: str) -> DingTalkInteraction | None: ...

    def latest_needs_human(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> DingTalkInteraction | None: ...

    def latest_awaiting_clarification(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> DingTalkInteraction | None: ...

    def close_clarification(
        self,
        ticket_code: str,
    ) -> DingTalkInteraction | None: ...

    def attach_collaboration_group(
        self,
        *,
        ticket_code: str,
        group_id: str,
        group_name: str,
    ) -> DingTalkInteraction | None: ...

    def claim_interaction(
        self,
        *,
        ticket_code: str,
        owner: str,
        owner_id: str | None = None,
    ) -> DingTalkInteraction | None: ...

    def answer_interaction(
        self,
        *,
        ticket_code: str,
        owner_id: str,
        final_answer: str,
    ) -> DingTalkInteraction | None: ...

    def review_answer(
        self,
        *,
        ticket_code: str,
        owner_id: str,
        reviewed_by: str,
        knowledge_status: KnowledgeReviewStatus,
    ) -> DingTalkInteraction | None: ...

    def resolve_interaction(
        self,
        *,
        ticket_code: str,
        owner: str,
        final_answer: str,
        reviewed_by: str,
        knowledge_status: KnowledgeReviewStatus,
    ) -> DingTalkInteraction | None: ...


class InMemoryDingTalkState:
    """Lightweight state implementation used by unit tests and local probes."""

    def __init__(self) -> None:
        self._processed_message_ids: set[str] = set()
        self._conversation_ids: dict[tuple[str, str], str] = {}
        self._domains: dict[tuple[str, str], BusinessDomain] = {}
        self._interactions: list[DingTalkInteraction] = []

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed_message_ids

    def mark_processed(self, message_id: str) -> None:
        self._processed_message_ids.add(message_id)

    def get_conversation_id(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> str | None:
        return self._conversation_ids.get((sender_id, dingtalk_conversation_id))

    def set_conversation_id(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        backend_conversation_id: str,
    ) -> None:
        self._conversation_ids[(sender_id, dingtalk_conversation_id)] = (
            backend_conversation_id
        )

    def get_domain(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> BusinessDomain | None:
        return self._domains.get((sender_id, dingtalk_conversation_id))

    def set_domain(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        domain: BusinessDomain,
    ) -> None:
        self._domains[(sender_id, dingtalk_conversation_id)] = domain

    def record_interaction(
        self,
        *,
        message: DingTalkInboundMessage,
        answer: EnterpriseAnswer,
    ) -> DingTalkInteraction:
        now = int(time.time())
        interaction_id = len(self._interactions) + 1
        interaction = DingTalkInteraction(
            interaction_id=interaction_id,
            ticket_code=_ticket_code(interaction_id, now),
            message_id=message.message_id.strip(),
            sender_id=message.sender_id.strip(),
            conversation_id=message.conversation_id.strip(),
            channel=message.channel,
            question=redact_question(message.text),
            answer=answer.text,
            domain=answer.domain,
            action=answer.action,
            reason=answer.reason,
            feedback=None,
            status=_initial_interaction_status(answer),
            owner=None,
            owner_id=None,
            final_answer=None,
            reviewed_by=None,
            knowledge_status="not_reviewed",
            collaboration_group_id=None,
            collaboration_group_name=None,
            collaboration_status=None,
            created_at=now,
            updated_at=now,
        )
        self._interactions.append(interaction)
        return interaction

    def record_feedback(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        feedback: InteractionFeedback,
    ) -> DingTalkInteraction | None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        for index in range(len(self._interactions) - 1, -1, -1):
            interaction = self._interactions[index]
            if (
                interaction.sender_id != sender
                or interaction.conversation_id != conversation
                or interaction.status
                not in ("awaiting_feedback", "needs_human", "resolved")
                or interaction.status == "resolved"
                and interaction.final_answer is None
            ):
                continue
            updated = _with_feedback(interaction, feedback)
            self._interactions[index] = updated
            return updated
        return None

    def list_interactions(
        self,
        *,
        status: InteractionStatus | None = None,
    ) -> tuple[DingTalkInteraction, ...]:
        if status is None:
            return tuple(self._interactions)
        return tuple(
            interaction
            for interaction in self._interactions
            if interaction.status == status
        )

    def get_interaction(self, ticket_code: str) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        return next(
            (
                interaction
                for interaction in self._interactions
                if interaction.ticket_code == normalized_ticket
            ),
            None,
        )

    def latest_needs_human(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> DingTalkInteraction | None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        return next(
            (
                interaction
                for interaction in reversed(self._interactions)
                if interaction.sender_id == sender
                and interaction.conversation_id == conversation
                and interaction.status == "needs_human"
            ),
            None,
        )

    def latest_awaiting_clarification(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> DingTalkInteraction | None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        return next(
            (
                interaction
                for interaction in reversed(self._interactions)
                if interaction.sender_id == sender
                and interaction.conversation_id == conversation
                and interaction.status == "awaiting_clarification"
            ),
            None,
        )

    def close_clarification(
        self,
        ticket_code: str,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        for index, interaction in enumerate(self._interactions):
            if interaction.ticket_code != normalized_ticket:
                continue
            if interaction.status != "awaiting_clarification":
                return None
            updated = replace(
                interaction,
                status="closed",
                updated_at=int(time.time()),
            )
            self._interactions[index] = updated
            return updated
        return None

    def attach_collaboration_group(
        self,
        *,
        ticket_code: str,
        group_id: str,
        group_name: str,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_group_id = _require_text(group_id, "group_id")
        normalized_group_name = _require_text(group_name, "group_name")
        for index, interaction in enumerate(self._interactions):
            if interaction.ticket_code != normalized_ticket:
                continue
            if interaction.status != "needs_human":
                return None
            updated = replace(
                interaction,
                collaboration_group_id=normalized_group_id,
                collaboration_group_name=normalized_group_name,
                collaboration_status="active",
                updated_at=int(time.time()),
            )
            self._interactions[index] = updated
            return updated
        return None

    def claim_interaction(
        self,
        *,
        ticket_code: str,
        owner: str,
        owner_id: str | None = None,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_owner = _require_text(owner, "owner")
        normalized_owner_id = (
            _require_text(owner_id, "owner_id") if owner_id is not None else None
        )
        for index, interaction in enumerate(self._interactions):
            if interaction.ticket_code != normalized_ticket:
                continue
            same_owner = (
                interaction.owner_id == normalized_owner_id
                if normalized_owner_id is not None
                else interaction.owner == normalized_owner
            )
            if (
                interaction.status != "needs_human"
                or interaction.owner is not None
                and not same_owner
            ):
                return None
            updated = replace(
                interaction,
                owner=normalized_owner,
                owner_id=normalized_owner_id,
                updated_at=int(time.time()),
            )
            self._interactions[index] = updated
            return updated
        return None

    def answer_interaction(
        self,
        *,
        ticket_code: str,
        owner_id: str,
        final_answer: str,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_owner_id = _require_text(owner_id, "owner_id")
        normalized_answer = _require_text(final_answer, "final_answer")
        for index, interaction in enumerate(self._interactions):
            if interaction.ticket_code != normalized_ticket:
                continue
            if (
                interaction.status != "needs_human"
                or interaction.owner_id != normalized_owner_id
            ):
                return None
            updated = replace(
                interaction,
                final_answer=normalized_answer,
                status="resolved",
                updated_at=int(time.time()),
            )
            self._interactions[index] = updated
            return updated
        return None

    def review_answer(
        self,
        *,
        ticket_code: str,
        owner_id: str,
        reviewed_by: str,
        knowledge_status: KnowledgeReviewStatus,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_owner_id = _require_text(owner_id, "owner_id")
        normalized_reviewer = _require_text(reviewed_by, "reviewed_by")
        normalized_knowledge_status = _require_knowledge_status(knowledge_status)
        for index, interaction in enumerate(self._interactions):
            if interaction.ticket_code != normalized_ticket:
                continue
            if (
                interaction.status != "resolved"
                or interaction.owner_id != normalized_owner_id
                or interaction.knowledge_status != "not_reviewed"
            ):
                return None
            updated = replace(
                interaction,
                reviewed_by=normalized_reviewer,
                knowledge_status=normalized_knowledge_status,
                updated_at=int(time.time()),
            )
            self._interactions[index] = updated
            return updated
        return None

    def resolve_interaction(
        self,
        *,
        ticket_code: str,
        owner: str,
        final_answer: str,
        reviewed_by: str,
        knowledge_status: KnowledgeReviewStatus,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_owner = _require_text(owner, "owner")
        normalized_answer = _require_text(final_answer, "final_answer")
        normalized_reviewer = _require_text(reviewed_by, "reviewed_by")
        normalized_knowledge_status = _require_knowledge_status(knowledge_status)
        for index, interaction in enumerate(self._interactions):
            if interaction.ticket_code != normalized_ticket:
                continue
            if (
                interaction.status != "needs_human"
                or interaction.owner != normalized_owner
            ):
                return None
            updated = replace(
                interaction,
                final_answer=normalized_answer,
                reviewed_by=normalized_reviewer,
                knowledge_status=normalized_knowledge_status,
                status="resolved",
                collaboration_status=(
                    "resolved"
                    if interaction.collaboration_group_id is not None
                    else None
                ),
                updated_at=int(time.time()),
            )
            self._interactions[index] = updated
            return updated
        return None


class SQLiteDingTalkState:
    """Durable message and Dify conversation state for the Stream worker."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def is_processed(self, message_id: str) -> bool:
        normalized_id = _require_text(message_id, "message_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM dingtalk_processed_messages
                WHERE message_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        return row is not None

    def mark_processed(self, message_id: str) -> None:
        normalized_id = _require_text(message_id, "message_id")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO dingtalk_processed_messages (
                    message_id, processed_at
                ) VALUES (?, ?)
                """,
                (normalized_id, int(time.time())),
            )

    def get_conversation_id(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> str | None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT backend_conversation_id
                FROM dingtalk_conversations
                WHERE sender_id = ? AND conversation_id = ?
                """,
                (sender, conversation),
            ).fetchone()
        return None if row is None else row["backend_conversation_id"]

    def set_conversation_id(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        backend_conversation_id: str,
    ) -> None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        backend = _require_text(backend_conversation_id, "backend_conversation_id")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dingtalk_conversations (
                    sender_id, conversation_id, backend_conversation_id,
                    domain, updated_at
                ) VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(sender_id, conversation_id) DO UPDATE SET
                    backend_conversation_id = excluded.backend_conversation_id,
                    updated_at = excluded.updated_at
                """,
                (sender, conversation, backend, int(time.time())),
            )

    def get_domain(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> BusinessDomain | None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT domain
                FROM dingtalk_conversations
                WHERE sender_id = ? AND conversation_id = ?
                """,
                (sender, conversation),
            ).fetchone()
        domain = None if row is None else row["domain"]
        return domain if domain in _BUSINESS_DOMAINS else None

    def set_domain(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        domain: BusinessDomain,
    ) -> None:
        if domain not in _BUSINESS_DOMAINS:
            raise ValueError("domain is invalid.")
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dingtalk_conversations (
                    sender_id, conversation_id, backend_conversation_id,
                    domain, updated_at
                ) VALUES (?, ?, NULL, ?, ?)
                ON CONFLICT(sender_id, conversation_id) DO UPDATE SET
                    domain = excluded.domain,
                    updated_at = excluded.updated_at
                """,
                (sender, conversation, domain, int(time.time())),
            )

    def record_interaction(
        self,
        *,
        message: DingTalkInboundMessage,
        answer: EnterpriseAnswer,
    ) -> DingTalkInteraction:
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO dingtalk_interactions (
                    ticket_code, message_id, sender_id, conversation_id,
                    channel, question, answer, domain, action, reason,
                    feedback, status, owner, final_answer, reviewed_by,
                    knowledge_status, created_at, updated_at
                ) VALUES (
                    '', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL,
                    NULL, 'not_reviewed', ?, ?
                )
                """,
                (
                    message.message_id.strip(),
                    message.sender_id.strip(),
                    message.conversation_id.strip(),
                    message.channel,
                    redact_question(message.text),
                    answer.text,
                    answer.domain,
                    answer.action,
                    answer.reason,
                    _initial_interaction_status(answer),
                    now,
                    now,
                ),
            )
            interaction_id = int(cursor.lastrowid)
            ticket_code = _ticket_code(interaction_id, now)
            connection.execute(
                """
                UPDATE dingtalk_interactions
                SET ticket_code = ?
                WHERE interaction_id = ?
                """,
                (ticket_code, interaction_id),
            )
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE interaction_id = ?
                """,
                (interaction_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Recorded DingTalk interaction could not be loaded.")
        return _row_to_interaction(row)

    def record_feedback(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
        feedback: InteractionFeedback,
    ) -> DingTalkInteraction | None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        now = int(time.time())
        status: InteractionStatus = (
            "resolved" if feedback == "resolved" else "needs_human"
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT interaction_id
                FROM dingtalk_interactions
                WHERE sender_id = ?
                  AND conversation_id = ?
                  AND (
                    status IN ('awaiting_feedback', 'needs_human')
                    OR (status = 'resolved' AND final_answer IS NOT NULL)
                  )
                ORDER BY interaction_id DESC
                LIMIT 1
                """,
                (sender, conversation),
            ).fetchone()
            if row is None:
                return None
            interaction_id = int(row["interaction_id"])
            connection.execute(
                """
                UPDATE dingtalk_interactions
                SET feedback = ?, status = ?,
                    owner = CASE WHEN ? = 'resolved' THEN owner ELSE NULL END,
                    owner_id = CASE WHEN ? = 'resolved' THEN owner_id ELSE NULL END,
                    reviewed_by = CASE
                        WHEN ? = 'resolved' THEN reviewed_by ELSE NULL
                    END,
                    knowledge_status = CASE
                        WHEN ? = 'resolved' THEN knowledge_status
                        ELSE 'not_reviewed'
                    END,
                    collaboration_status = CASE
                        WHEN ? = 'resolved'
                             AND collaboration_group_id IS NOT NULL
                        THEN 'resolved'
                        WHEN collaboration_group_id IS NOT NULL
                        THEN 'active'
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE interaction_id = ?
                """,
                (
                    feedback,
                    status,
                    feedback,
                    feedback,
                    feedback,
                    feedback,
                    feedback,
                    now,
                    interaction_id,
                ),
            )
            updated = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE interaction_id = ?
                """,
                (interaction_id,),
            ).fetchone()
        return None if updated is None else _row_to_interaction(updated)

    def list_interactions(
        self,
        *,
        status: InteractionStatus | None = None,
    ) -> tuple[DingTalkInteraction, ...]:
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM dingtalk_interactions
                    ORDER BY interaction_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM dingtalk_interactions
                    WHERE status = ?
                    ORDER BY interaction_id
                    """,
                    (status,),
                ).fetchall()
        return tuple(_row_to_interaction(row) for row in rows)

    def get_interaction(self, ticket_code: str) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE ticket_code = ?
                """,
                (normalized_ticket,),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def latest_needs_human(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> DingTalkInteraction | None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE sender_id = ?
                  AND conversation_id = ?
                  AND status = 'needs_human'
                ORDER BY interaction_id DESC
                LIMIT 1
                """,
                (sender, conversation),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def latest_awaiting_clarification(
        self,
        *,
        sender_id: str,
        dingtalk_conversation_id: str,
    ) -> DingTalkInteraction | None:
        sender = _require_text(sender_id, "sender_id")
        conversation = _require_text(
            dingtalk_conversation_id,
            "dingtalk_conversation_id",
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE sender_id = ?
                  AND conversation_id = ?
                  AND status = 'awaiting_clarification'
                ORDER BY interaction_id DESC
                LIMIT 1
                """,
                (sender, conversation),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def close_clarification(
        self,
        ticket_code: str,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE dingtalk_interactions
                SET status = 'closed', updated_at = ?
                WHERE ticket_code = ?
                  AND status = 'awaiting_clarification'
                """,
                (now, normalized_ticket),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE ticket_code = ?
                """,
                (normalized_ticket,),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def attach_collaboration_group(
        self,
        *,
        ticket_code: str,
        group_id: str,
        group_name: str,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_group_id = _require_text(group_id, "group_id")
        normalized_group_name = _require_text(group_name, "group_name")
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE dingtalk_interactions
                SET collaboration_group_id = ?, collaboration_group_name = ?,
                    collaboration_status = 'active', updated_at = ?
                WHERE ticket_code = ? AND status = 'needs_human'
                """,
                (
                    normalized_group_id,
                    normalized_group_name,
                    now,
                    normalized_ticket,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE ticket_code = ?
                """,
                (normalized_ticket,),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def claim_interaction(
        self,
        *,
        ticket_code: str,
        owner: str,
        owner_id: str | None = None,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_owner = _require_text(owner, "owner")
        normalized_owner_id = (
            _require_text(owner_id, "owner_id") if owner_id is not None else None
        )
        now = int(time.time())
        with self._connect() as connection:
            if normalized_owner_id is None:
                cursor = connection.execute(
                    """
                    UPDATE dingtalk_interactions
                    SET owner = ?, updated_at = ?
                    WHERE ticket_code = ?
                      AND status = 'needs_human'
                      AND (owner IS NULL OR owner = ?)
                    """,
                    (
                        normalized_owner,
                        now,
                        normalized_ticket,
                        normalized_owner,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE dingtalk_interactions
                    SET owner = ?, owner_id = ?, updated_at = ?
                    WHERE ticket_code = ?
                      AND status = 'needs_human'
                      AND (
                        owner IS NULL
                        OR owner_id = ?
                        OR (owner_id IS NULL AND owner = ?)
                      )
                    """,
                    (
                        normalized_owner,
                        normalized_owner_id,
                        now,
                        normalized_ticket,
                        normalized_owner_id,
                        normalized_owner,
                    ),
                )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE ticket_code = ?
                """,
                (normalized_ticket,),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def answer_interaction(
        self,
        *,
        ticket_code: str,
        owner_id: str,
        final_answer: str,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_owner_id = _require_text(owner_id, "owner_id")
        normalized_answer = _require_text(final_answer, "final_answer")
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE dingtalk_interactions
                SET final_answer = ?, status = 'resolved', updated_at = ?
                WHERE ticket_code = ?
                  AND status = 'needs_human'
                  AND owner_id = ?
                """,
                (
                    normalized_answer,
                    now,
                    normalized_ticket,
                    normalized_owner_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE ticket_code = ?
                """,
                (normalized_ticket,),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def review_answer(
        self,
        *,
        ticket_code: str,
        owner_id: str,
        reviewed_by: str,
        knowledge_status: KnowledgeReviewStatus,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_owner_id = _require_text(owner_id, "owner_id")
        normalized_reviewer = _require_text(reviewed_by, "reviewed_by")
        normalized_knowledge_status = _require_knowledge_status(knowledge_status)
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE dingtalk_interactions
                SET reviewed_by = ?, knowledge_status = ?, updated_at = ?
                WHERE ticket_code = ?
                  AND status = 'resolved'
                  AND owner_id = ?
                  AND knowledge_status = 'not_reviewed'
                """,
                (
                    normalized_reviewer,
                    normalized_knowledge_status,
                    now,
                    normalized_ticket,
                    normalized_owner_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE ticket_code = ?
                """,
                (normalized_ticket,),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def resolve_interaction(
        self,
        *,
        ticket_code: str,
        owner: str,
        final_answer: str,
        reviewed_by: str,
        knowledge_status: KnowledgeReviewStatus,
    ) -> DingTalkInteraction | None:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_owner = _require_text(owner, "owner")
        normalized_answer = _require_text(final_answer, "final_answer")
        normalized_reviewer = _require_text(reviewed_by, "reviewed_by")
        normalized_knowledge_status = _require_knowledge_status(knowledge_status)
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE dingtalk_interactions
                SET final_answer = ?, reviewed_by = ?, knowledge_status = ?,
                    status = 'resolved',
                    collaboration_status = CASE
                        WHEN collaboration_group_id IS NOT NULL THEN 'resolved'
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE ticket_code = ?
                  AND status = 'needs_human'
                  AND owner = ?
                """,
                (
                    normalized_answer,
                    normalized_reviewer,
                    normalized_knowledge_status,
                    now,
                    normalized_ticket,
                    normalized_owner,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM dingtalk_interactions
                WHERE ticket_code = ?
                """,
                (normalized_ticket,),
            ).fetchone()
        return None if row is None else _row_to_interaction(row)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS dingtalk_processed_messages (
                    message_id TEXT PRIMARY KEY,
                    processed_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dingtalk_conversations (
                    sender_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    backend_conversation_id TEXT,
                    domain TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(sender_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS dingtalk_interactions (
                    interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_code TEXT NOT NULL UNIQUE,
                    message_id TEXT NOT NULL UNIQUE,
                    sender_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT,
                    feedback TEXT,
                    status TEXT NOT NULL,
                    owner TEXT,
                    owner_id TEXT,
                    final_answer TEXT,
                    reviewed_by TEXT,
                    knowledge_status TEXT NOT NULL DEFAULT 'not_reviewed',
                    collaboration_group_id TEXT,
                    collaboration_group_name TEXT,
                    collaboration_status TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dingtalk_interactions_status
                ON dingtalk_interactions(status, interaction_id);
                CREATE INDEX IF NOT EXISTS idx_dingtalk_interactions_conversation
                ON dingtalk_interactions(
                    sender_id, conversation_id, interaction_id
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(dingtalk_interactions)"
                ).fetchall()
            }
            if "owner_id" not in columns:
                connection.execute(
                    "ALTER TABLE dingtalk_interactions ADD COLUMN owner_id TEXT"
                )
            for column_name, column_type in (
                ("collaboration_group_id", "TEXT"),
                ("collaboration_group_name", "TEXT"),
                ("collaboration_status", "TEXT"),
            ):
                if column_name not in columns:
                    connection.execute(
                        f"ALTER TABLE dingtalk_interactions "
                        f"ADD COLUMN {column_name} {column_type}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dingtalk_collaboration_active
                ON dingtalk_interactions(
                    sender_id, domain, collaboration_status, interaction_id
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class DingTalkAssistantAdapter:
    """Translate validated DingTalk events into channel-independent questions."""

    def __init__(
        self,
        *,
        assistant: EnterpriseAssistant,
        state: DingTalkState,
    ) -> None:
        self._assistant = assistant
        self._state = state

    def handle(self, message: DingTalkInboundMessage) -> EnterpriseAnswer | None:
        if message.sender_is_bot:
            return None

        self._validate(message)
        message_id = message.message_id.strip()
        if self._state.is_processed(message_id):
            return None

        sender_id = message.sender_id.strip()
        dingtalk_conversation_id = message.conversation_id.strip()
        feedback = _parse_feedback(message.text)
        if feedback is not None:
            updated = self._state.record_feedback(
                sender_id=sender_id,
                dingtalk_conversation_id=dingtalk_conversation_id,
                feedback=feedback,
            )
            self._state.mark_processed(message_id)
            if updated is None:
                return EnterpriseAnswer(
                    domain="unknown",
                    action="clarify",
                    text=(
                        "暂时没有找到需要评价的上一条业务回答。"
                        "请先提出具体业务问题。"
                    ),
                    reason="missing_business_context",
                )
            if feedback == "resolved":
                return EnterpriseAnswer(
                    domain=updated.domain,
                    action="allow",
                    text="已记录为“已解决”，感谢反馈。",
                )
            return EnterpriseAnswer(
                domain=updated.domain,
                action="handoff",
                text=(
                    f"已记录问题 {updated.ticket_code}，"
                    "后续需要由对应负责人核实处理。"
                ),
                reason="knowledge_gap",
            )

        backend_conversation_id = self._state.get_conversation_id(
            sender_id=sender_id,
            dingtalk_conversation_id=dingtalk_conversation_id,
        )
        domain_hint = self._state.get_domain(
            sender_id=sender_id,
            dingtalk_conversation_id=dingtalk_conversation_id,
        )
        pending_clarification = self._state.latest_awaiting_clarification(
            sender_id=sender_id,
            dingtalk_conversation_id=dingtalk_conversation_id,
        )
        answer = self._assistant.answer(
            message.text,
            user=f"dingtalk:{sender_id}",
            conversation_id=backend_conversation_id,
            domain_hint=domain_hint,
            prior_question=(
                pending_clarification.question
                if pending_clarification is not None
                else None
            ),
            channel=message.channel,
        )

        if answer.conversation_id:
            self._state.set_conversation_id(
                sender_id=sender_id,
                dingtalk_conversation_id=dingtalk_conversation_id,
                backend_conversation_id=answer.conversation_id,
            )
        if answer.action in ("allow", "clarify") and answer.domain != "unknown":
            self._state.set_domain(
                sender_id=sender_id,
                dingtalk_conversation_id=dingtalk_conversation_id,
                domain=answer.domain,
            )
        recorded_message = message
        if pending_clarification is not None and answer.action != "clarify":
            recorded_message = replace(
                message,
                text=_format_question_chain(
                    pending_clarification.question,
                    pending_clarification.answer,
                    message.text,
                ),
            )
        interaction = self._state.record_interaction(
            message=recorded_message,
            answer=answer,
        )
        if pending_clarification is not None and answer.action != "clarify":
            self._state.close_clarification(
                pending_clarification.ticket_code
            )
        self._state.mark_processed(message_id)
        if answer.action == "allow" and answer.domain != "unknown":
            return _replace_answer_text(
                answer,
                (
                    f"{answer.text}\n\n"
                    "这条回答是否解决了问题？"
                    "请回复“已解决”或“未解决”。"
                ),
            )
        if answer.action == "handoff":
            return _answer_with_ticket(answer, interaction.ticket_code)
        return answer

    def record_failure(
        self,
        message: DingTalkInboundMessage,
        answer: EnterpriseAnswer,
    ) -> EnterpriseAnswer | None:
        if message.sender_is_bot:
            return None
        self._validate(message)
        message_id = message.message_id.strip()
        if self._state.is_processed(message_id):
            return None
        interaction = self._state.record_interaction(
            message=message,
            answer=answer,
        )
        self._state.mark_processed(message_id)
        return _answer_with_ticket(answer, interaction.ticket_code)

    @staticmethod
    def _validate(message: DingTalkInboundMessage) -> None:
        required_fields = (
            ("message_id", message.message_id),
            ("sender_id", message.sender_id),
            ("conversation_id", message.conversation_id),
            ("text", message.text),
        )
        for field_name, value in required_fields:
            if not value.strip():
                raise ValueError(f"{field_name} is required.")
        if message.channel not in ("group", "direct"):
            raise ValueError("channel must be 'group' or 'direct'.")


_BUSINESS_DOMAINS = {
    "hr",
    "shareholder",
    "activity",
    "finance",
    "operations",
    "unknown",
}


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required.")
    return value.strip()


def _format_question_chain(
    original_question: str,
    clarification_question: str,
    follow_up: str,
) -> str:
    original = " ".join(original_question.split())
    clarification = " ".join(clarification_question.split())
    supplement = " ".join(follow_up.split())
    if not original:
        return supplement
    if not supplement or supplement == original:
        return original
    lines = [f"员工最初提问：{original}"]
    if clarification:
        lines.append(f"机器人追问：{clarification}")
    lines.append(f"员工补充：{supplement}")
    return "\n".join(lines)


def _initial_interaction_status(answer: EnterpriseAnswer) -> InteractionStatus:
    if answer.action == "handoff":
        return "needs_human"
    if answer.action == "clarify":
        return "awaiting_clarification"
    if answer.action == "allow" and answer.domain != "unknown":
        return "awaiting_feedback"
    return "closed"


def _parse_feedback(text: str) -> InteractionFeedback | None:
    normalized = "".join(text.casefold().split()).strip(
        "，。；、,.!?！？~～"
    )
    if normalized in {
        "已解决",
        "解决了",
        "有帮助",
        "回答正确",
        "正确",
    }:
        return "resolved"
    if normalized in {
        "未解决",
        "没解决",
        "没有解决",
        "回答不对",
        "不对",
        "没用",
    } or any(
        phrase in normalized
        for phrase in (
            "未解决我的问题",
            "没解决我的问题",
            "没有解决我的问题",
            "仍未解决",
            "还是没解决",
            "回答没有解决",
        )
    ):
        return "unresolved"
    if normalized in {
        "转人工",
        "需要人工",
        "联系人工",
        "人工",
    }:
        return "human_requested"
    return None


def _replace_answer_text(
    answer: EnterpriseAnswer,
    text: str,
) -> EnterpriseAnswer:
    return EnterpriseAnswer(
        domain=answer.domain,
        action=answer.action,
        text=text,
        reason=answer.reason,
        conversation_id=answer.conversation_id,
    )


def _answer_with_ticket(
    answer: EnterpriseAnswer,
    ticket_code: str,
) -> EnterpriseAnswer:
    return _replace_answer_text(
        answer,
        f"{answer.text}\n\n问题已记录：{ticket_code}。",
    )


def _with_feedback(
    interaction: DingTalkInteraction,
    feedback: InteractionFeedback,
) -> DingTalkInteraction:
    reopen = feedback != "resolved"
    return DingTalkInteraction(
        interaction_id=interaction.interaction_id,
        ticket_code=interaction.ticket_code,
        message_id=interaction.message_id,
        sender_id=interaction.sender_id,
        conversation_id=interaction.conversation_id,
        channel=interaction.channel,
        question=interaction.question,
        answer=interaction.answer,
        domain=interaction.domain,
        action=interaction.action,
        reason=interaction.reason,
        feedback=feedback,
        status="resolved" if feedback == "resolved" else "needs_human",
        owner=None if reopen else interaction.owner,
        owner_id=None if reopen else interaction.owner_id,
        final_answer=interaction.final_answer,
        reviewed_by=None if reopen else interaction.reviewed_by,
        knowledge_status=(
            "not_reviewed" if reopen else interaction.knowledge_status
        ),
        collaboration_group_id=interaction.collaboration_group_id,
        collaboration_group_name=interaction.collaboration_group_name,
        collaboration_status=(
            "resolved"
            if feedback == "resolved" and interaction.collaboration_group_id
            else "active"
            if interaction.collaboration_group_id
            else None
        ),
        created_at=interaction.created_at,
        updated_at=int(time.time()),
    )


def _ticket_code(interaction_id: int, created_at: int) -> str:
    date = time.strftime("%Y%m%d", time.localtime(created_at))
    return f"CS-{date}-{interaction_id:06d}"


def _require_knowledge_status(value: str) -> KnowledgeReviewStatus:
    normalized = _require_text(value, "knowledge_status")
    if normalized not in {"approved", "not_reusable"}:
        raise ValueError("knowledge_status is invalid.")
    return normalized


def _row_to_interaction(row: sqlite3.Row) -> DingTalkInteraction:
    return DingTalkInteraction(
        interaction_id=int(row["interaction_id"]),
        ticket_code=str(row["ticket_code"]),
        message_id=str(row["message_id"]),
        sender_id=str(row["sender_id"]),
        conversation_id=str(row["conversation_id"]),
        channel=row["channel"],
        question=str(row["question"]),
        answer=str(row["answer"]),
        domain=row["domain"],
        action=row["action"],
        reason=row["reason"],
        feedback=row["feedback"],
        status=row["status"],
        owner=row["owner"],
        owner_id=row["owner_id"],
        final_answer=row["final_answer"],
        reviewed_by=row["reviewed_by"],
        knowledge_status=str(row["knowledge_status"]),
        collaboration_group_id=row["collaboration_group_id"],
        collaboration_group_name=row["collaboration_group_name"],
        collaboration_status=row["collaboration_status"],
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )
