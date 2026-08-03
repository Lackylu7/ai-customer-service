from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence, cast

from app.enterprise_policy import BusinessDomain, EnterprisePolicy, PolicyAction


_DOMAIN_PREFIXES: Mapping[str, BusinessDomain] = {
    "HR": "hr",
    "SH": "shareholder",
    "A": "activity",
    "F": "finance",
}


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    question: str
    status: str
    expected_domain: BusinessDomain
    expected_keywords: tuple[str, ...]
    forbidden: tuple[str, ...]


@dataclass(frozen=True)
class AnswerCheck:
    passed: bool
    missing_keywords: tuple[str, ...]
    forbidden_hits: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationReport:
    total: int
    routed_correctly: int
    action_counts: dict[PolicyAction, int]
    domain_counts: dict[BusinessDomain, int]
    status_counts: dict[str, int]
    mismatched_ids: tuple[str, ...]


def load_evaluation_cases(paths: Sequence[str | Path]) -> tuple[EvaluationCase, ...]:
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} contains invalid JSON.") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_number} must contain an object.")
            case_id = _required_text(payload, "id", path, line_number)
            if case_id in seen_ids:
                raise ValueError(f"duplicate case ID {case_id}.")
            seen_ids.add(case_id)
            prefix = case_id.split("-", 1)[0]
            expected_domain = _DOMAIN_PREFIXES.get(prefix)
            if expected_domain is None:
                raise ValueError(f"{path}:{line_number} has an unknown case ID prefix.")
            cases.append(
                EvaluationCase(
                    case_id=case_id,
                    question=_required_text(payload, "question", path, line_number),
                    status=_required_text(payload, "status", path, line_number),
                    expected_domain=expected_domain,
                    expected_keywords=_optional_texts(
                        payload,
                        "expected_keywords",
                        path,
                        line_number,
                    ),
                    forbidden=_optional_texts(
                        payload,
                        "forbidden",
                        path,
                        line_number,
                    ),
                )
            )
    return tuple(cases)


def evaluate_cases(
    cases: Iterable[EvaluationCase],
    policy: EnterprisePolicy,
) -> EvaluationReport:
    case_list = tuple(cases)
    mismatches: list[str] = []
    actions: Counter[PolicyAction] = Counter()
    domains: Counter[BusinessDomain] = Counter()
    statuses: Counter[str] = Counter()
    for case in case_list:
        decision = policy.evaluate(case.question)
        actions[decision.action] += 1
        domains[case.expected_domain] += 1
        statuses[case.status] += 1
        if decision.domain != case.expected_domain:
            mismatches.append(case.case_id)
    return EvaluationReport(
        total=len(case_list),
        routed_correctly=len(case_list) - len(mismatches),
        action_counts=dict(actions),
        domain_counts=dict(domains),
        status_counts=dict(statuses),
        mismatched_ids=tuple(mismatches),
    )


def check_answer(case: EvaluationCase, answer: str) -> AnswerCheck:
    normalized_answer = _normalize_for_match(answer)
    missing = tuple(
        keyword
        for keyword in case.expected_keywords
        if not _contains_unnegated_phrase(
            normalized_answer,
            _normalize_for_match(keyword),
        )
    )
    forbidden_hits = tuple(
        phrase
        for phrase in case.forbidden
        if _contains_unnegated_phrase(
            normalized_answer,
            _normalize_for_match(phrase),
        )
    )
    return AnswerCheck(
        passed=not missing and not forbidden_hits,
        missing_keywords=missing,
        forbidden_hits=forbidden_hits,
    )


def _required_text(
    payload: Mapping[str, object],
    field: str,
    path: Path,
    line_number: int,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}:{line_number} field {field} must be text.")
    return cast(str, value.strip())


def _optional_texts(
    payload: Mapping[str, object],
    field: str,
    path: Path,
    line_number: int,
) -> tuple[str, ...]:
    value = payload.get(field, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{path}:{line_number} field {field} must be a text array.")
    return tuple(item.strip() for item in value)


def _normalize_for_match(text: str) -> str:
    return "".join(text.casefold().split())


def _contains_unnegated_phrase(answer: str, phrase: str) -> bool:
    start = 0
    negations = ("不", "不能", "不可", "不得", "不要", "禁止", "避免", "无法", "拒绝")
    boundaries = "，。；！？,.!?;但"
    while True:
        index = answer.find(phrase, start)
        if index < 0:
            return False
        clause_start = max(answer.rfind(boundary, 0, index) for boundary in boundaries)
        prefix = answer[clause_start + 1 : index]
        if not any(negation in prefix for negation in negations):
            return True
        start = index + max(1, len(phrase))
