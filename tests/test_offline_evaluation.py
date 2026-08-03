import json
import tempfile
import unittest
from pathlib import Path

from app.enterprise_policy import EnterprisePolicy
from app.offline_evaluation import (
    EvaluationCase,
    check_answer,
    evaluate_cases,
    load_evaluation_cases,
)


class OfflineEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_evaluates_routing_and_preflight_actions_from_jsonl(self) -> None:
        path = self.root / "cases.jsonl"
        rows = (
            {"id": "HR-Q001", "question": "教练入职要做什么？", "status": "ready_for_test"},
            {"id": "SH-Q001", "question": "股东入股怎么走？", "status": "ready_for_test"},
            {"id": "A-Q001", "question": "68折活动周期怎么算？", "status": "ready_for_test"},
            {"id": "F-Q001", "question": "报销从哪里发起？", "status": "ready_for_test"},
        )
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

        report = evaluate_cases(
            load_evaluation_cases([path]),
            EnterprisePolicy(),
        )

        self.assertEqual(report.total, 4)
        self.assertEqual(report.routed_correctly, 4)
        self.assertEqual(report.action_counts, {"allow": 4})
        self.assertEqual(
            report.domain_counts,
            {"hr": 1, "shareholder": 1, "activity": 1, "finance": 1},
        )
        self.assertEqual(report.status_counts, {"ready_for_test": 4})
        self.assertEqual(report.mismatched_ids, ())

    def test_rejects_duplicate_case_ids_across_files(self) -> None:
        first = self.root / "first.jsonl"
        second = self.root / "second.jsonl"
        row = {"id": "HR-Q001", "question": "入职怎么做？", "status": "ready_for_test"}
        first.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        second.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "duplicate case ID HR-Q001"):
            load_evaluation_cases([first, second])

    def test_checks_required_and_forbidden_answer_phrases(self) -> None:
        path = self.root / "answer-case.jsonl"
        path.write_text(
            json.dumps(
                {
                    "id": "F-Q001",
                    "question": "报销从哪里发起？",
                    "status": "ready_for_test",
                    "expected_keywords": ["工作台", "审批"],
                    "forbidden": ["保证通过"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        case = load_evaluation_cases([path])[0]

        passing = check_answer(case, "请在工作台的审批入口发起。")
        failing = check_answer(case, "我保证通过，请直接提交。")

        self.assertTrue(passing.passed)
        self.assertEqual(passing.missing_keywords, ())
        self.assertEqual(passing.forbidden_hits, ())
        self.assertFalse(failing.passed)
        self.assertEqual(failing.missing_keywords, ("工作台", "审批"))
        self.assertEqual(failing.forbidden_hits, ("保证通过",))

        negated_forbidden = EvaluationCase(
            case_id="A-Q001",
            question="能参加吗？",
            status="ready_for_test",
            expected_domain="activity",
            expected_keywords=(),
            forbidden=("可以",),
        )
        self.assertTrue(check_answer(negated_forbidden, "不可以参加该活动。").passed)

    def test_negated_required_keywords_do_not_count_as_correct_answers(self) -> None:
        case = EvaluationCase(
            case_id="F-Q001",
            question="报销需要什么？",
            status="ready_for_test",
            expected_domain="finance",
            expected_keywords=("OA", "发票"),
            forbidden=(),
        )

        result = check_answer(case, "不需要走 OA，也不用发票。")

        self.assertFalse(result.passed)
        self.assertEqual(result.missing_keywords, ("OA", "发票"))


if __name__ == "__main__":
    unittest.main()
