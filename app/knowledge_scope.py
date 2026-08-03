from dataclasses import dataclass
from datetime import date
from typing import Literal


KnowledgeStatus = Literal["draft", "pending_review", "active", "expired", "disabled"]
KnowledgeDomain = Literal["hr", "shareholder", "activity", "finance"]
KnowledgeAudience = Literal[
    "employee",
    "store_leader",
    "shareholder",
    "headquarters",
    "customer",
]
EligibilityReason = Literal[
    "eligible",
    "not_started",
    "expired",
    "not_active",
    "restricted_audience",
    "missing_context",
    "out_of_scope",
]


_ALL_AUDIENCES: frozenset[KnowledgeAudience] = frozenset(
    {"employee", "store_leader", "shareholder", "headquarters", "customer"}
)


@dataclass(frozen=True)
class KnowledgeItem:
    knowledge_id: str
    domain: KnowledgeDomain
    status: KnowledgeStatus
    active_from: date | None = None
    active_until: date | None = None
    visible_to: frozenset[KnowledgeAudience] = _ALL_AUDIENCES
    applicable_cities: frozenset[str] = frozenset()
    applicable_stores: frozenset[str] = frozenset()


@dataclass(frozen=True)
class KnowledgeContext:
    as_of: date
    audience: KnowledgeAudience = "employee"
    city: str | None = None
    store: str | None = None


@dataclass(frozen=True)
class KnowledgeEligibility:
    eligible: bool
    reason: EligibilityReason


class KnowledgeScope:
    def evaluate(
        self,
        item: KnowledgeItem,
        context: KnowledgeContext,
    ) -> KnowledgeEligibility:
        if context.audience not in item.visible_to:
            return KnowledgeEligibility(
                eligible=False,
                reason="restricted_audience",
            )
        if item.status != "active":
            return KnowledgeEligibility(eligible=False, reason="not_active")
        if item.active_from is not None and context.as_of < item.active_from:
            return KnowledgeEligibility(eligible=False, reason="not_started")
        if item.active_until is not None and context.as_of > item.active_until:
            return KnowledgeEligibility(eligible=False, reason="expired")
        if item.applicable_cities:
            if context.city is None:
                return KnowledgeEligibility(eligible=False, reason="missing_context")
            if context.city not in item.applicable_cities:
                return KnowledgeEligibility(eligible=False, reason="out_of_scope")
        if item.applicable_stores:
            if context.store is None:
                return KnowledgeEligibility(eligible=False, reason="missing_context")
            if context.store not in item.applicable_stores:
                return KnowledgeEligibility(eligible=False, reason="out_of_scope")
        return KnowledgeEligibility(eligible=True, reason="eligible")
