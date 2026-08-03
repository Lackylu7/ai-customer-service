from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
from typing import Iterable, Literal, Mapping

from app.knowledge_scope import KnowledgeDomain


SourceAuthority = Literal[
    "official_policy",
    "owner_confirmed",
    "operational",
    "draft",
    "historical",
]
SourceReviewStatus = Literal["approved", "pending_review", "rejected"]
SourceLifecycle = Literal["current", "superseded", "stopped"]
SourceSensitivity = Literal["internal", "restricted_personal", "confidential"]
SourceUse = Literal[
    "direct_answer",
    "reference_only",
    "historical_only",
    "restricted_lookup",
    "blocked",
]


@dataclass(frozen=True)
class KnowledgeSource:
    source_id: str
    title: str
    domain: KnowledgeDomain
    authority: SourceAuthority
    review_status: SourceReviewStatus
    lifecycle: SourceLifecycle = "current"
    sensitivity: SourceSensitivity = "internal"
    effective_from: date | None = None
    effective_until: date | None = None
    owner: str | None = None
    supersedes: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    source_url: str | None = None
    last_verified_at: date | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("Knowledge source ID is required.")
        if not self.title.strip():
            raise ValueError("Knowledge source title is required.")
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_from > self.effective_until
        ):
            raise ValueError("Knowledge source effective window is invalid.")


@dataclass(frozen=True)
class KnowledgeSourceDecision:
    use: SourceUse
    include_content: bool
    reason: str


@dataclass(frozen=True)
class KnowledgeSectionRule:
    heading_contains: str
    use: SourceUse
    reason: str

    def __post_init__(self) -> None:
        if not self.heading_contains.strip():
            raise ValueError("Knowledge section heading matcher is required.")
        if not self.reason.strip():
            raise ValueError("Knowledge section rule reason is required.")


@dataclass(frozen=True)
class KnowledgeSourceDocument:
    source: KnowledgeSource
    markdown: str
    lookup_hint: str | None = None
    section_rules: tuple[KnowledgeSectionRule, ...] = ()


@dataclass(frozen=True)
class LayeredKnowledgePackage:
    domain: KnowledgeDomain
    layer: SourceUse
    source_ids: tuple[str, ...]
    markdown: str


@dataclass(frozen=True)
class DirectAnswerCandidate:
    domain: KnowledgeDomain
    source_ids: tuple[str, ...]
    markdown: str


@dataclass(frozen=True)
class _KnowledgeFragment:
    source: KnowledgeSource
    heading: str | None
    markdown: str
    rule_reason: str | None = None
    lookup_hint: str | None = None


class KnowledgeSourcePolicy:
    """Decides how a source may participate in enterprise answers."""

    def evaluate(
        self,
        source: KnowledgeSource,
        *,
        as_of: date,
    ) -> KnowledgeSourceDecision:
        if source.sensitivity == "confidential":
            return KnowledgeSourceDecision("blocked", False, "confidential")
        if source.sensitivity == "restricted_personal":
            return KnowledgeSourceDecision(
                "restricted_lookup",
                False,
                "identity_scoped_lookup_required",
            )
        if source.conflicts_with:
            return KnowledgeSourceDecision("blocked", False, "source_conflict")
        if source.review_status == "rejected":
            return KnowledgeSourceDecision("blocked", False, "review_rejected")
        if source.lifecycle in {"stopped", "superseded"}:
            return KnowledgeSourceDecision("historical_only", True, source.lifecycle)
        if source.authority == "historical":
            return KnowledgeSourceDecision("historical_only", True, "historical")
        if source.effective_until is not None and as_of > source.effective_until:
            return KnowledgeSourceDecision("historical_only", True, "expired")
        if source.effective_from is not None and as_of < source.effective_from:
            return KnowledgeSourceDecision("reference_only", True, "not_started")
        if source.review_status == "pending_review" or source.authority == "draft":
            return KnowledgeSourceDecision("reference_only", True, "not_approved")
        return KnowledgeSourceDecision("direct_answer", True, "approved_current")


_LAYER_ORDER: tuple[SourceUse, ...] = (
    "direct_answer",
    "reference_only",
    "historical_only",
    "restricted_lookup",
)


def build_layered_knowledge_packages(
    documents: Iterable[KnowledgeSourceDocument],
    *,
    as_of: date,
    policy: KnowledgeSourcePolicy | None = None,
) -> tuple[LayeredKnowledgePackage, ...]:
    evaluator = policy or KnowledgeSourcePolicy()
    grouped: dict[tuple[KnowledgeDomain, SourceUse], list[_KnowledgeFragment]] = {}

    for document in documents:
        decision = evaluator.evaluate(document.source, as_of=as_of)
        if decision.use == "blocked":
            continue
        if decision.use == "restricted_lookup":
            grouped.setdefault((document.source.domain, decision.use), []).append(
                _KnowledgeFragment(
                    source=document.source,
                    heading=None,
                    markdown="",
                    lookup_hint=document.lookup_hint,
                )
            )
            continue
        if not document.markdown.strip():
            raise ValueError(
                f"Knowledge source {document.source.source_id} has empty content."
            )
        for heading, markdown in _split_markdown_sections(document.markdown):
            layer, rule_reason = _section_layer(
                decision.use,
                heading,
                document.section_rules,
            )
            if layer == "blocked":
                continue
            grouped.setdefault((document.source.domain, layer), []).append(
                _KnowledgeFragment(
                    source=document.source,
                    heading=heading,
                    markdown=markdown,
                    rule_reason=rule_reason,
                )
            )

    packages: list[LayeredKnowledgePackage] = []
    domains: tuple[KnowledgeDomain, ...] = (
        "hr",
        "shareholder",
        "activity",
        "finance",
    )
    for domain in domains:
        for layer in _LAYER_ORDER:
            layer_fragments = sorted(
                grouped.get((domain, layer), ()),
                key=lambda item: (item.source.source_id, item.heading or ""),
            )
            if not layer_fragments:
                continue
            sections = [_render_fragment(fragment, layer) for fragment in layer_fragments]
            source_ids = tuple(
                dict.fromkeys(fragment.source.source_id for fragment in layer_fragments)
            )
            packages.append(
                LayeredKnowledgePackage(
                    domain=domain,
                    layer=layer,
                    source_ids=source_ids,
                    markdown=(
                        f"# {domain} knowledge sources: {layer}\n\n"
                        f"> layer={layer}; source_count={len(source_ids)}\n\n"
                        + "\n\n".join(sections)
                        + "\n"
                    ),
                )
            )
    return tuple(packages)


def write_layered_knowledge_packages(
    packages: Iterable[LayeredKnowledgePackage],
    output_directory: str | Path,
) -> tuple[Path, ...]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for package in packages:
        path = output / f"{package.domain}-{package.layer}.md"
        path.write_text(package.markdown, encoding="utf-8")
        written.append(path)
    return tuple(written)


def build_direct_answer_candidate(
    *,
    domain: KnowledgeDomain,
    approved_faq_markdown: str,
    layered_packages: Iterable[LayeredKnowledgePackage],
    blocked_markers: dict[str, tuple[str, ...]],
) -> DirectAnswerCandidate:
    if not approved_faq_markdown.strip():
        raise ValueError("Approved FAQ knowledge is required.")
    direct_packages = tuple(
        package
        for package in layered_packages
        if package.domain == domain and package.layer == "direct_answer"
    )
    source_ids = tuple(
        dict.fromkeys(
            source_id
            for package in direct_packages
            for source_id in package.source_ids
        )
    )
    source_markdown = "\n\n".join(
        package.markdown.strip() for package in direct_packages
    )
    source_markdown = re.sub(
        r"(?m)^###\s+(.+?)\s*$",
        lambda match: f"**{match.group(1)}**",
        source_markdown,
    )
    source_markdown = re.sub(r"(?m)^##\s+", "### ", source_markdown)
    markdown = (
        f"# {domain} direct-answer candidate\n\n"
        "## 已审核 FAQ\n\n"
        f"{approved_faq_markdown.strip()}\n"
    )
    if source_markdown:
        markdown += f"\n## 已放行正式资料章节\n\n{source_markdown}\n"
    validate_direct_answer_packages(
        (
            LayeredKnowledgePackage(
                domain=domain,
                layer="direct_answer",
                source_ids=source_ids,
                markdown=markdown,
            ),
        ),
        blocked_markers,
    )
    return DirectAnswerCandidate(
        domain=domain,
        source_ids=source_ids,
        markdown=markdown,
    )


def validate_direct_answer_packages(
    packages: Iterable[LayeredKnowledgePackage],
    blocked_markers: dict[str, tuple[str, ...]],
) -> None:
    direct_text = "\n".join(
        package.markdown
        for package in packages
        if package.layer == "direct_answer"
    )
    normalized_text = "".join(direct_text.casefold().split())
    violations: list[str] = []
    for knowledge_id, markers in blocked_markers.items():
        if any(
            marker.strip()
            and "".join(marker.casefold().split()) in normalized_text
            for marker in markers
        ):
            violations.append(knowledge_id)
    if violations:
        raise ValueError(
            "Frozen knowledge appeared in direct-answer packages: "
            + ", ".join(sorted(violations))
        )


_FAQ_SECTION = re.compile(
    r"^###\s+(SH-Q\d{3}).*?(?=^###\s+SH-Q\d{3}|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)


def validate_faq_case_coverage(
    candidate_markdown: str,
    cases: Iterable[Mapping[str, object]],
) -> None:
    sections = {
        match.group(1): "".join(match.group(0).casefold().split())
        for match in _FAQ_SECTION.finditer(candidate_markdown)
    }
    failed: list[str] = []
    for case in cases:
        case_id = case.get("id")
        keywords = case.get("expected_keywords")
        if not isinstance(case_id, str) or not isinstance(keywords, list):
            raise ValueError("FAQ retrieval cases require an ID and keyword list.")
        if any(not isinstance(keyword, str) for keyword in keywords):
            raise ValueError("FAQ retrieval keywords must be text.")
        canonical_id = re.sub(r"-NL\d+$", "", case_id)
        section = sections.get(canonical_id, "")
        if not section or any(
            "".join(keyword.casefold().split()) not in section
            for keyword in keywords
        ):
            failed.append(case_id)
    if failed:
        raise ValueError(
            "FAQ candidate missed retrieval cases: " + ", ".join(sorted(failed))
        )


_H2_HEADING = re.compile(r"^##\s+(.+?)\s*$", flags=re.MULTILINE)


def _split_markdown_sections(markdown: str) -> tuple[tuple[str | None, str], ...]:
    matches = tuple(_H2_HEADING.finditer(markdown))
    if not matches:
        return ((None, markdown.strip()),)
    sections: list[tuple[str | None, str]] = []
    preface = markdown[: matches[0].start()].strip()
    if preface:
        sections.append((None, preface))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections.append((match.group(1).strip(), markdown[match.start() : end].strip()))
    return tuple(sections)


def _section_layer(
    base_use: SourceUse,
    heading: str | None,
    rules: tuple[KnowledgeSectionRule, ...],
) -> tuple[SourceUse, str | None]:
    if heading is None:
        return base_use, None
    normalized_heading = heading.casefold()
    for rule in rules:
        if rule.heading_contains.casefold() not in normalized_heading:
            continue
        requested = rule.use
        if requested == "blocked":
            return "blocked", rule.reason
        if base_use == "historical_only":
            return "historical_only", rule.reason
        if base_use == "reference_only" and requested == "direct_answer":
            return "reference_only", rule.reason
        if base_use == "restricted_lookup":
            return "restricted_lookup", rule.reason
        return requested, rule.reason
    return base_use, None


def _render_fragment(fragment: _KnowledgeFragment, layer: SourceUse) -> str:
    source = fragment.source
    metadata = (
        f"source_id={source.source_id}; authority={source.authority}; "
        f"review_status={source.review_status}; lifecycle={source.lifecycle}; "
        f"sensitivity={source.sensitivity}; answer_layer={layer}"
    )
    if fragment.rule_reason:
        metadata += f"; section_rule_reason={fragment.rule_reason}"
    if layer == "restricted_lookup":
        lookup_hint = fragment.lookup_hint or "需要完成身份与字段权限校验后查询"
        body = f"查询说明：{lookup_hint}"
    else:
        body = fragment.markdown.strip()
        if fragment.heading:
            body = re.sub(r"^##\s+.+?(?:\r?\n|$)", "", body, count=1).lstrip()
    section_suffix = f" / {fragment.heading}" if fragment.heading else ""
    return f"## {source.title}{section_suffix}\n\n> {metadata}\n\n{body}"
