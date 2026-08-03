"""Real Dify checks for colloquial shareholder questions.

Run explicitly after publishing a candidate workflow. The regular unit-test
suite stays offline and does not import this file.
"""

from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.dingtalk_mvp_agent import build_brain, load_env_file


def main() -> int:
    load_env_file()
    client = build_brain()
    cases = (
        (
            "钱已经打过去了，从什么时候能开始算我那份收益啊？",
            (("分红",), ("起始", "起算", "开始")),
        ),
        (
            "每个月赚到的那部分我上哪儿瞅？",
            (("损益表",), ("股东群",)),
        ),
        (
            "店里的十五万那个安全垫，经营中能不能拿出来？",
            (("风险金",), ("不能",)),
        ),
        (
            "报表里人工咋比大家工资加起来还多？",
            (("人工成本",), ("社保",)),
        ),
    )
    failures: list[str] = []
    for index, (question, required_groups) in enumerate(cases, 1):
        response = client.send_message(
            query=question,
            user=f"v04-natural-language-e2e-{index}",
        )
        source_names = {
            resource.get("document_name")
            for resource in response.retriever_resources
        }
        if "enterprise-knowledge-shareholder.md" not in source_names:
            failures.append(f"case {index}: approved shareholder source missing")
            continue
        missing = [
            alternatives
            for alternatives in required_groups
            if not any(term in response.answer for term in alternatives)
        ]
        if missing:
            failures.append(
                f"case {index}: answer missing required meaning groups {missing!r}: "
                f"{response.answer}"
            )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: Dify understands four colloquial shareholder questions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
