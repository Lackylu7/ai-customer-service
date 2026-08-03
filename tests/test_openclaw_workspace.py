import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "openclaw" / "shareholder-3-0"


class OpenClawWorkspaceTests(unittest.TestCase):
    def test_workspace_has_required_bootstrap_and_knowledge_files(self) -> None:
        required = (
            "AGENTS.md",
            "IDENTITY.md",
            "SOUL.md",
            "USER.md",
            "knowledge/approved-shareholder-3-0.md",
            "acceptance-cases.jsonl",
        )
        for relative_path in required:
            with self.subTest(path=relative_path):
                self.assertTrue((WORKSPACE / relative_path).is_file())

    def test_acceptance_set_has_19_approved_and_8_pending_unique_cases(self) -> None:
        cases = [
            json.loads(line)
            for line in (WORKSPACE / "acceptance-cases.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

        self.assertEqual(len(cases), 27)
        self.assertEqual(len({case["id"] for case in cases}), 27)
        self.assertEqual(
            sum(case["status"] == "ready_for_test" for case in cases),
            19,
        )
        self.assertEqual(
            sum(case["status"] == "pending_review" for case in cases),
            8,
        )

    def test_workspace_contains_no_secret_assignment_patterns(self) -> None:
        content = "\n".join(
            path.read_text(encoding="utf-8")
            for path in WORKSPACE.rglob("*")
            if path.is_file()
        ).lower()
        for marker in ("api_key=", "appsecret=", "token=", "password="):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, content)


if __name__ == "__main__":
    unittest.main()
