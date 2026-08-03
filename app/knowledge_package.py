from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Sequence

from app.knowledge_scope import KnowledgeDomain
from app.offline_evaluation import EvaluationCase


_ANSWER_SECTION = re.compile(
    r"^###\s+((?:HR|SH|A|F)-Q\d{3})[：:].*?(?=^###\s+|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)
_DOMAIN_ORDER: tuple[KnowledgeDomain, ...] = (
    "hr",
    "shareholder",
    "activity",
    "finance",
)


@dataclass(frozen=True)
class KnowledgePackage:
    domain: KnowledgeDomain
    case_ids: tuple[str, ...]
    markdown: str


def build_knowledge_packages(
    answer_bank_paths: Sequence[str | Path],
    cases: Iterable[EvaluationCase],
) -> tuple[KnowledgePackage, ...]:
    sections = _load_answer_sections(answer_bank_paths)
    ready_cases = sorted(
        (case for case in cases if case.status == "ready_for_test"),
        key=lambda case: case.case_id,
    )
    missing = tuple(case.case_id for case in ready_cases if case.case_id not in sections)
    if missing:
        raise ValueError(
            "ready cases are missing from answer banks: " + ", ".join(missing)
        )

    packages: list[KnowledgePackage] = []
    for domain in _DOMAIN_ORDER:
        domain_cases = tuple(
            case for case in ready_cases if case.expected_domain == domain
        )
        if not domain_cases:
            continue
        case_ids = tuple(case.case_id for case in domain_cases)
        content = "\n\n".join(sections[case_id].strip() for case_id in case_ids)
        markdown = (
            f"# {domain} enterprise knowledge package\n\n"
            "> 仅包含状态为 ready_for_test 的知识条目；其他条目必须保持冻结。\n\n"
            f"> 条目数：{len(case_ids)}\n\n"
            f"{content}\n"
        )
        packages.append(
            KnowledgePackage(
                domain=domain,
                case_ids=case_ids,
                markdown=markdown,
            )
        )
    return tuple(packages)


def write_knowledge_packages(
    packages: Iterable[KnowledgePackage],
    output_directory: str | Path,
) -> tuple[Path, ...]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for package in packages:
        path = output / f"enterprise-knowledge-{package.domain}.md"
        path.write_text(package.markdown, encoding="utf-8")
        written.append(path)
    return tuple(written)


def _load_answer_sections(
    answer_bank_paths: Sequence[str | Path],
) -> dict[str, str]:
    sections: dict[str, str] = {}
    for raw_path in answer_bank_paths:
        path = Path(raw_path)
        text = path.read_text(encoding="utf-8-sig")
        for match in _ANSWER_SECTION.finditer(text):
            case_id = match.group(1)
            if case_id in sections:
                raise ValueError(f"duplicate answer section {case_id}.")
            sections[case_id] = match.group(0)
    return sections
