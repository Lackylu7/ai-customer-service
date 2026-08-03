"""Real Dify checks for general and personal store-expansion questions."""

from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.dingtalk_mvp_agent import build_brain, load_env_file
from app.enterprise_assistant import EnterpriseAssistant


def main() -> int:
    load_env_file()
    assistant = EnterpriseAssistant(brain=build_brain())
    cases = (
        (
            "现在的制度下，达成多少指标才能开下一家店？",
            ("回本", "70%", "独立族长"),
        ),
        (
            "我原店已经回本，最近两个月平均业绩是80%，也已经有正式任命的独立族长，"
            "我能开下一家店了吗？",
            ("回本", "业绩", "独立族长"),
        ),
    )
    failures: list[str] = []
    for index, (question, required_terms) in enumerate(cases, 1):
        result = assistant.answer(
            question,
            user=f"v06-store-expansion-e2e-{index}",
            channel="direct",
        )
        missing = [term for term in required_terms if term not in result.text]
        if result.action != "allow":
            failures.append(
                f"case {index}: expected allow, got {result.action}/{result.reason}"
            )
        elif missing:
            failures.append(f"case {index}: answer missing required terms {missing!r}")
        elif "风险金" in result.text:
            failures.append(f"case {index}: answer drifted to risk-fund rules")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: Dify handles general and personal store-expansion questions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
