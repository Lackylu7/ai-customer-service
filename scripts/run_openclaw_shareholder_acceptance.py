from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from app.openclaw_brain import OpenClawShareholderBrain


def load_cases(path: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw_line.strip():
            continue
        value = json.loads(raw_line)
        if not isinstance(value, dict):
            raise ValueError(f"Line {line_number} must contain a JSON object.")
        cases.append(value)
    return cases


def evaluate_case(case: dict[str, object], answer: str) -> list[str]:
    failures: list[str] = []
    status = case.get("status")
    expected = case.get("expected_keywords", [])
    forbidden = case.get("forbidden", [])
    if status == "pending_review":
        if not answer.startswith("[HANDOFF]"):
            failures.append("pending case did not hand off")
        return failures
    if status != "ready_for_test":
        failures.append("invalid case status")
        return failures
    if answer.startswith("[HANDOFF]"):
        failures.append("approved case handed off")
    if isinstance(expected, list):
        for keyword in expected:
            if isinstance(keyword, str) and keyword not in answer:
                failures.append(f"missing keyword: {keyword}")
    if isinstance(forbidden, list):
        for phrase in forbidden:
            if isinstance(phrase, str) and phrase and phrase in answer:
                failures.append(f"forbidden phrase: {phrase}")
    return failures


def run_case(
    case: dict[str, object],
    *,
    executable: str,
    agent_id: str,
    thinking: str,
    timeout_seconds: int,
) -> dict[str, object]:
    case_id = str(case["id"])
    question = str(case["question"])
    command = [
        executable,
        "agent",
        "--agent",
        agent_id,
        "--message",
        question,
        "--session-id",
        f"acceptance-{case_id.lower()}",
        "--thinking",
        thinking,
        "--timeout",
        str(timeout_seconds),
        "--json",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds + 5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "id": case_id,
            "passed": False,
            "failures": [type(exc).__name__],
            "answer": "",
        }
    answer = OpenClawShareholderBrain._extract_reply(completed.stdout)
    failures = (
        [f"openclaw exit status {completed.returncode}"]
        if completed.returncode != 0
        else evaluate_case(case, answer)
    )
    return {
        "id": case_id,
        "passed": not failures,
        "failures": failures,
        "answer": answer,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("openclaw/shareholder-3-0/acceptance-cases.jsonl"),
    )
    parser.add_argument("--executable", default="openclaw")
    parser.add_argument("--agent", default="shareholder-3-0")
    parser.add_argument("--thinking", default="low")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    results = [
        run_case(
            case,
            executable=arguments.executable,
            agent_id=arguments.agent,
            thinking=arguments.thinking,
            timeout_seconds=arguments.timeout,
        )
        for case in load_cases(arguments.cases)
    ]
    report = {
        "total": len(results),
        "passed": sum(1 for result in results if result["passed"]),
        "failed": sum(1 for result in results if not result["passed"]),
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output is not None:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
