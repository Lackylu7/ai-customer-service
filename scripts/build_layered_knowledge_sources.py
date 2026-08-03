import argparse
from datetime import date
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from app.knowledge_sources import (
    KnowledgeSource,
    KnowledgeSourceDocument,
    KnowledgeSourcePolicy,
    KnowledgeSectionRule,
    build_direct_answer_candidate,
    build_layered_knowledge_packages,
    validate_direct_answer_packages,
    write_layered_knowledge_packages,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build authority-layered knowledge packages from a DWS source registry."
    )
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--approved-faq", type=Path)
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8-sig"))
    as_of = args.as_of or date.fromisoformat(registry["as_of"])
    documents = tuple(_load_document(item) for item in registry["sources"])
    packages = build_layered_knowledge_packages(documents, as_of=as_of)
    blocked_markers = {
        item["knowledge_id"]: tuple(item.get("answer_markers", ()))
        for item in registry.get("blocked_topics", ())
    }
    validate_direct_answer_packages(packages, blocked_markers)
    written = write_layered_knowledge_packages(packages, args.output)
    combined_path: Path | None = None
    if args.approved_faq is not None:
        candidate = build_direct_answer_candidate(
            domain=registry.get("domain", "shareholder"),
            approved_faq_markdown=args.approved_faq.read_text(encoding="utf-8-sig"),
            layered_packages=packages,
            blocked_markers=blocked_markers,
        )
        combined_path = args.output / f"{candidate.domain}-direct-answer-candidate.md"
        combined_path.write_text(candidate.markdown, encoding="utf-8")
    _write_manifest(args.output, packages, as_of, combined_path)

    for path in written:
        print(path)
    if combined_path is not None:
        print(combined_path)
    return 0


def _load_document(raw: dict[str, Any]) -> KnowledgeSourceDocument:
    section_rules = tuple(
        KnowledgeSectionRule(
            heading_contains=item["heading_contains"],
            use=item["use"],
            reason=item["reason"],
        )
        for item in raw.get("section_rules", ())
    )
    source = KnowledgeSource(
        source_id=raw["source_id"],
        title=raw["title"],
        domain=raw.get("domain", "shareholder"),
        authority=raw["authority"],
        review_status=raw["review_status"],
        lifecycle=raw.get("lifecycle", "current"),
        sensitivity=raw.get("sensitivity", "internal"),
        effective_from=_optional_date(raw.get("effective_from")),
        effective_until=_optional_date(raw.get("effective_until")),
        owner=raw.get("owner_role"),
        conflicts_with=tuple(raw.get("conflicts_with", ())),
        last_verified_at=date.today(),
    )
    decision = KnowledgeSourcePolicy().evaluate(source, as_of=date.today())
    if decision.use == "restricted_lookup":
        return KnowledgeSourceDocument(
            source=source,
            markdown="",
            lookup_hint=raw.get("lookup_hint") or raw.get("notes"),
            section_rules=section_rules,
        )

    payload = _read_dingtalk_document(raw["node_id"])
    markdown = payload.get("markdown", "")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError(f"DingTalk source {source.source_id} returned no markdown.")
    return KnowledgeSourceDocument(
        source=source,
        markdown=markdown,
        section_rules=section_rules,
    )


def _read_dingtalk_document(node_id: str) -> dict[str, Any]:
    dws_executable = shutil.which("dws")
    dws_script = shutil.which("dws.ps1")
    if dws_executable:
        command = [
            dws_executable,
            "doc",
            "read",
            "--node",
            node_id,
            "--format",
            "json",
        ]
    elif dws_script:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-File",
            dws_script,
            "doc",
            "read",
            "--node",
            node_id,
            "--format",
            "json",
        ]
    else:
        command = ["dws", "doc", "read", "--node", node_id, "--format", "json"]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)
    if not payload.get("success"):
        raise RuntimeError(f"DWS failed to read DingTalk node {node_id}.")
    return payload


def _write_manifest(
    output: Path,
    packages: tuple[Any, ...],
    as_of: date,
    combined_path: Path | None,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": "0.5",
        "as_of": as_of.isoformat(),
        "packages": [
            {
                "file": f"{package.domain}-{package.layer}.md",
                "domain": package.domain,
                "layer": package.layer,
                "source_ids": list(package.source_ids),
            }
            for package in packages
        ],
        "combined_direct_answer": combined_path.name if combined_path else None,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Knowledge source dates must be ISO strings.")
    return date.fromisoformat(value)


if __name__ == "__main__":
    raise SystemExit(main())
