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


if __name__ == "__main__":
    unittest.main()
