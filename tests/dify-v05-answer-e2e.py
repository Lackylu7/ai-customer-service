"""Real final-answer checks for the isolated V0.5 Dify test app."""

from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.dingtalk_mvp_agent import build_brain


EXPECTED_SOURCE = "shareholder-direct-answer-candidate-v0.5.md"


def main() -> int:
    env_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        ".runtime/customer-service/v05-test.env"
    )
    client = build_brain(_read_env(env_path))
    failures: list[str] = []

    answer_cases = (
        ("你好", ("好",), False),
        (
            "钱已经打过去了，从什么时候能开始算我那份收益啊？",
            ("分红", "起始"),
            True,
        ),
        (
            "店里的十五万那个安全垫，经营中能不能拿出来？",
            ("风险金", "不能"),
            True,
        ),
        ("差评删掉以后处罚还算吗？", ("撤销",), True),
        ("新店和老店至少要隔多远？", ("1.5", "大众点评"), True),
        (
            "报表里人工咋比大家工资加起来还多？",
            ("人工成本", "社保"),
            True,
        ),
    )
    for index, (question, required, require_source) in enumerate(answer_cases, 1):
        response = client.send_message(query=question, user=f"v05-answer-{index}")
        missing = tuple(term for term in required if term not in response.answer)
        if missing:
            failures.append(f"answer {index}: missing {missing!r}: {response.answer}")
        if require_source and EXPECTED_SOURCE not in _source_names(response):
            failures.append(f"answer {index}: approved V0.5 source missing")
        if question == "你好" and "[HANDOFF]" in response.answer:
            failures.append("answer 1: greeting incorrectly handed off")

    frozen = client.send_message(
        query="我还差多少才能再开一家店？",
        user="v05-frozen-answer",
    )
    if not any(
        term in frozen.answer
        for term in (
            "[HANDOFF]",
            "不足",
            "负责人",
            "核实",
            "无法确认",
            "无法直接判断",
            "没有相关规则",
            "需要了解更多",
        )
    ):
        failures.append(f"frozen answer did not declare uncertainty: {frozen.answer}")
    for forbidden in ("原门店分红已回本", "平均业绩达到", "独立族长"):
        if forbidden in frozen.answer:
            failures.append(f"frozen answer leaked {forbidden!r}")
    if "3个月内" in frozen.answer and not any(
        term in frozen.answer
        for term in ("并不能", "并没有", "并未", "无法确认", "不能回答")
    ):
        failures.append(
            "frozen answer presented an adjacent rule without separating it: "
            + frozen.answer
        )

    first = client.send_message(
        query="风险金是做什么的？",
        user="v05-multiturn-risk",
    )
    follow_up = client.send_message(
        query="那正常经营的时候我能拿出来吗？",
        user="v05-multiturn-risk",
        conversation_id=first.conversation_id,
    )
    if "不能" not in follow_up.answer or "风险金" not in follow_up.answer:
        failures.append(f"risk follow-up lost context: {follow_up.answer}")

    types = client.send_message(
        query="公司有哪些股东类型？",
        user="v05-multiturn-types",
    )
    type_follow_up = client.send_message(
        query="那管理股东最多能持有多少？",
        user="v05-multiturn-types",
        conversation_id=types.conversation_id,
    )
    if "40%" not in type_follow_up.answer:
        failures.append(f"shareholder follow-up lost context: {type_follow_up.answer}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: V0.5 final answers 9/9 (6 single-turn, 1 frozen, 2 multi-turn).")
    return 0


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"").strip("'")
    return values


def _source_names(response: object) -> set[str]:
    resources = getattr(response, "retriever_resources", ())
    return {
        str(resource.get("document_name"))
        for resource in resources
        if isinstance(resource, dict)
    }


if __name__ == "__main__":
    raise SystemExit(main())
