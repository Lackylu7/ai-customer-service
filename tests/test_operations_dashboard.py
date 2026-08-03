import unittest
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)
from fastapi.testclient import TestClient

from app.main import app


class OperationsDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def test_operations_page_is_served(self) -> None:
        response = self.client.get("/operations")

        self.assertEqual(response.status_code, 200)
        self.assertIn("RAG 客服运营看板", response.text)
        self.assertIn('lang="zh-CN"', response.text)

    def test_dashboard_has_no_external_assets_or_unsafe_html_rendering(self) -> None:
        source = Path("app/static/operations.html").read_text(encoding="utf-8")

        self.assertNotIn("https://cdn", source)
        self.assertNotIn("<script src=", source)
        self.assertNotIn("localStorage", source)
        self.assertNotIn(".innerHTML", source)
        self.assertIn("sessionStorage", source)
        self.assertIn('"X-Operations-Key"', source)
        self.assertIn("sessionStorage.getItem(KEY_NAME) === requestKey", source)
        self.assertNotIn("?key=", source)
        self.assertIn("textContent", source)

    def test_dashboard_contains_required_controls_and_responsive_layout(self) -> None:
        source = Path("app/static/operations.html").read_text(encoding="utf-8")

        required_ids = (
            "keyDialog",
            "keyInput",
            "windowControls",
            "kpiGrid",
            "trendChart",
            "reasonChart",
            "gapStatus",
            "gapList",
            "previousPage",
            "nextPage",
            "refreshButton",
            "exportEvents",
            "exportGaps",
        )
        for element_id in required_ids:
            self.assertIn(f'id="{element_id}"', source)
        for window in ("7d", "30d", "all"):
            self.assertIn(f'data-window="{window}"', source)
        self.assertIn("@media (max-width: 720px)", source)
        self.assertIn("overflow-x: hidden", source)
        self.assertIn("document.createElementNS", source)
        self.assertIn("data-queue-health", source)
        self.assertIn("data-worker-heartbeat", source)
        self.assertIn("data-failed-jobs", source)
        self.assertIn('id="operationsBrandName"', source)
        self.assertIn('id="chatwootLink"', source)
        self.assertIn('id="difyLink"', source)
        self.assertIn("/v1/operations/ui-config", source)
        self.assertIn("renderUiConfig", source)

    def test_dashboard_contains_safe_human_handoff_controls(self) -> None:
        source = Path("app/static/operations.html").read_text(encoding="utf-8")

        required_ids = (
            "humanConversationList",
            "humanConversationCount",
            "resumeDialog",
            "resumeConversationId",
            "resumeCancelButton",
            "resumeConfirmButton",
            "resumeError",
        )
        for element_id in required_ids:
            self.assertIn(f'id="{element_id}"', source)
        self.assertIn("/v1/operations/human-conversations", source)
        self.assertIn("/resume-ai", source)
        self.assertIn("data-resume-conversation", source)
        self.assertNotIn("window.confirm", source)
        self.assertNotIn(".innerHTML", source)

    def test_dashboard_contains_protected_knowledge_upload(self) -> None:
        source = Path("app/static/operations.html").read_text(encoding="utf-8")

        for element_id in (
            "knowledgeFiles",
            "uploadKnowledgeButton",
            "knowledgeUploadStatus",
            "knowledgeUploadResults",
        ):
            self.assertIn(f'id="{element_id}"', source)
        self.assertIn("/v1/knowledge/documents", source)
        self.assertIn("new FormData()", source)
        self.assertIn('Authorization: `Bearer ${operationsKey}`', source)
        self.assertIn("20 * 1024 * 1024", source)
        self.assertIn(".pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.md,.csv,.html,.htm", source)
        self.assertNotIn(".innerHTML", source)

    def test_dashboard_contains_dingtalk_human_review_workflow(self) -> None:
        source = Path("app/static/operations.html").read_text(encoding="utf-8")

        for element_id in (
            "dingtalkInteractionStatus",
            "dingtalkInteractionCount",
            "dingtalkInteractionList",
        ):
            self.assertIn(f'id="{element_id}"', source)
        self.assertIn("/v1/operations/dingtalk-interactions", source)
        self.assertIn("/claim", source)
        self.assertIn("/resolve", source)
        self.assertIn("确认已回复并完成", source)
        self.assertIn("可沉淀知识", source)
        self.assertIn("仅个案处理", source)
        self.assertNotIn(".innerHTML", source)


if __name__ == "__main__":
    unittest.main()
