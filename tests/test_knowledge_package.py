import tempfile
import unittest
from pathlib import Path

from app.knowledge_package import build_knowledge_packages, write_knowledge_packages
from app.offline_evaluation import EvaluationCase


class KnowledgePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_packages_only_ready_cases_and_preserves_answer_sections(self) -> None:
        answer_bank = self.root / "hr.md"
        answer_bank.write_text(
            "# 人事答案库\n\n"
            "### HR-Q001：入职流程是什么？\n\n"
            "- **状态**：可测试\n"
            "- **标准答案**：先完成招聘、培训和入职手续。\n\n"
            "### HR-Q002：审核时效多久？\n\n"
            "- **状态**：待确认\n"
            "- **标准答案**：当前时效待负责人确认。\n",
            encoding="utf-8",
        )
        cases = (
            self._case("HR-Q001", "ready_for_test"),
            self._case("HR-Q002", "needs_owner_confirmation"),
        )

        packages = build_knowledge_packages([answer_bank], cases)

        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].domain, "hr")
        self.assertEqual(packages[0].case_ids, ("HR-Q001",))
        self.assertIn("先完成招聘、培训和入职手续", packages[0].markdown)
        self.assertNotIn("HR-Q002", packages[0].markdown)
        self.assertNotIn("当前时效待负责人确认", packages[0].markdown)

    def test_rejects_ready_case_missing_from_answer_banks(self) -> None:
        answer_bank = self.root / "empty.md"
        answer_bank.write_text("# 空答案库\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "HR-Q001"):
            build_knowledge_packages(
                [answer_bank],
                (self._case("HR-Q001", "ready_for_test"),),
            )

    def test_writes_deterministic_domain_package_files(self) -> None:
        answer_bank = self.root / "hr.md"
        answer_bank.write_text(
            "### HR-Q001：入职流程是什么？\n\n- **标准答案**：完成入职手续。\n",
            encoding="utf-8",
        )
        packages = build_knowledge_packages(
            [answer_bank],
            (self._case("HR-Q001", "ready_for_test"),),
        )

        written = write_knowledge_packages(packages, self.root / "output")

        self.assertEqual(
            written,
            (self.root / "output" / "enterprise-knowledge-hr.md",),
        )
        self.assertEqual(
            written[0].read_text(encoding="utf-8"),
            packages[0].markdown,
        )

    @staticmethod
    def _case(case_id: str, status: str) -> EvaluationCase:
        return EvaluationCase(
            case_id=case_id,
            question="测试问题",
            status=status,
            expected_domain="hr",
            expected_keywords=(),
            forbidden=(),
        )


if __name__ == "__main__":
    unittest.main()
