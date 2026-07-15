import codecs
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)
from fastapi.testclient import TestClient

from app.operations_api import operations_router
from app.operations_store import OperationsStore


class OperationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "state.db"
        self.environment = patch.dict(
            os.environ,
            {"CUSTOMER_SERVICE_DB_PATH": str(self.database_path)},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("OPERATIONS_API_KEY", None)
        test_app = FastAPI()
        test_app.include_router(operations_router)
        self.client = TestClient(test_app)
        self.addCleanup(self.client.close)

    def configure_key(self) -> dict[str, str]:
        os.environ["OPERATIONS_API_KEY"] = "correct-secret"
        return {"X-Operations-Key": "correct-secret"}

    def test_returns_503_when_operations_key_is_not_configured(self) -> None:
        response = self.client.get("/v1/operations/summary?window=7d")

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("CUSTOMER_SERVICE_DB_PATH", response.text)

    def test_returns_401_for_missing_or_wrong_key(self) -> None:
        self.configure_key()

        missing = self.client.get("/v1/operations/summary?window=7d")
        wrong = self.client.get(
            "/v1/operations/summary?window=7d",
            headers={"X-Operations-Key": "wrong-secret"},
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn("correct-secret", wrong.text)

    def test_summary_returns_metrics_for_correct_key(self) -> None:
        headers = self.configure_key()
        OperationsStore(self.database_path).record_event(
            message_id=1,
            conversation_id=2,
            outcome="replied",
            reason=None,
            duration_ms=50,
        )

        response = self.client.get(
            "/v1/operations/summary?window=all",
            headers=headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["replied"], 1)
        self.assertEqual(response.json()["answer_rate"]["denominator"], 1)

    def test_invalid_filters_and_pagination_return_422(self) -> None:
        headers = self.configure_key()

        paths = (
            "/v1/operations/summary?window=90d",
            "/v1/operations/gaps?status=closed",
            "/v1/operations/gaps?limit=0",
            "/v1/operations/gaps?limit=201",
            "/v1/operations/gaps?offset=-1",
            "/v1/operations/export.csv?window=all&kind=unknown",
        )

        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get(path, headers=headers).status_code,
                    422,
                )

    def test_lists_and_updates_gap_with_strict_body(self) -> None:
        headers = self.configure_key()
        store = OperationsStore(self.database_path)
        store.record_knowledge_gap(
            question="保修多久？",
            conversation_id=9,
            created_at=100,
        )
        gap_id = store.list_gaps(status="all")[0][0].id

        listed = self.client.get(
            "/v1/operations/gaps?status=pending&limit=50&offset=0",
            headers=headers,
        )
        updated = self.client.patch(
            f"/v1/operations/gaps/{gap_id}",
            headers=headers,
            json={"status": "resolved", "note": "已补充保修条款"},
        )
        extra = self.client.patch(
            f"/v1/operations/gaps/{gap_id}",
            headers=headers,
            json={"status": "resolved", "note": "ok", "secret": "no"},
        )
        too_long = self.client.patch(
            f"/v1/operations/gaps/{gap_id}",
            headers=headers,
            json={"status": "resolved", "note": "n" * 1001},
        )
        missing = self.client.patch(
            "/v1/operations/gaps/999",
            headers=headers,
            json={"status": "ignored", "note": "not found"},
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 1)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["status"], "resolved")
        self.assertEqual(updated.json()["note"], "已补充保修条款")
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(too_long.status_code, 422)
        self.assertEqual(missing.status_code, 404)

    def test_exports_events_and_gaps_as_bom_prefixed_csv(self) -> None:
        headers = self.configure_key()
        store = OperationsStore(self.database_path)
        store.record_event(
            message_id=1,
            conversation_id=2,
            outcome="replied",
            reason=None,
            duration_ms=10,
        )
        store.record_knowledge_gap(
            question="发票怎么开？",
            conversation_id=2,
        )

        for kind in ("events", "gaps"):
            response = self.client.get(
                f"/v1/operations/export.csv?window=all&kind={kind}",
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(codecs.BOM_UTF8))
            self.assertIn("text/csv", response.headers["content-type"])
            self.assertIn(
                f"operations-{kind}-all.csv",
                response.headers["content-disposition"],
            )

    def test_main_application_mounts_operations_router(self) -> None:
        from app.main import app as main_app

        headers = self.configure_key()
        with TestClient(main_app) as client:
            response = client.get(
                "/v1/operations/summary?window=all",
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)

    def test_customer_service_starts_when_operations_store_initialization_fails(self) -> None:
        from app.main import get_customer_service
        from app.operations_models import NullOperationsRecorder
        from app.settings import Settings

        settings = Settings.from_env(
            {
                "DIFY_BASE_URL": "http://127.0.0.1:8081",
                "DIFY_APP_API_KEY": "test-dify-key",
                "CHATWOOT_BASE_URL": "http://127.0.0.1:3000",
                "CHATWOOT_ACCOUNT_ID": "1",
                "CHATWOOT_API_TOKEN": "test-chatwoot-token",
                "CHATWOOT_WEBHOOK_SECRET": "test-webhook-secret",
                "CUSTOMER_SERVICE_DB_PATH": str(self.database_path),
            }
        )
        get_customer_service.cache_clear()
        self.addCleanup(get_customer_service.cache_clear)

        with patch(
            "app.main.OperationsStore",
            side_effect=RuntimeError("operations schema unavailable"),
        ), self.assertLogs("app.main", level="WARNING") as logs:
            service = get_customer_service(settings)

        self.assertIsInstance(service._operations, NullOperationsRecorder)
        self.assertTrue(
            any("Operations store initialization failed" in line for line in logs.output)
        )
        self.assertFalse(any("test-dify-key" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
