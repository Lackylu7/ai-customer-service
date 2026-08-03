import codecs
import os
import sqlite3
import tempfile
import unittest
import warnings
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)
from fastapi.testclient import TestClient

from app.chatwoot_events import IncomingMessage
from app.dingtalk_adapter import (
    DingTalkInboundMessage,
    SQLiteDingTalkState,
)
from app.enterprise_assistant import EnterpriseAnswer
from app.handoff_control import (
    HandoffConflictError,
    HandoffNotFoundError,
    HandoffRemoteError,
    HandoffStorageError,
    ResumeResult,
)
from app.job_store import SQLiteJobStore
from app.operations_api import get_handoff_control_service, operations_router
from app.operations_store import OperationsStore
from app.settings import HandoffOperationsSettings


class OperationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "state.db"
        self.dingtalk_database_path = Path(self.temp_dir.name) / "dingtalk.db"
        self.environment = patch.dict(
            os.environ,
            {
                "CUSTOMER_SERVICE_DB_PATH": str(self.database_path),
                "DINGTALK_STATE_DB_PATH": str(self.dingtalk_database_path),
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("OPERATIONS_API_KEY", None)
        test_app = FastAPI()
        test_app.include_router(operations_router)
        self.client = TestClient(test_app)
        self.addCleanup(self.client.close)
        os.environ.pop("CHATWOOT_ACCOUNT_ID", None)
        os.environ.pop("CHATWOOT_API_TOKEN", None)

    def configure_key(self) -> dict[str, str]:
        os.environ["OPERATIONS_API_KEY"] = "correct-secret"
        return {"X-Operations-Key": "correct-secret"}

    def configure_handoff(self) -> dict[str, str]:
        headers = self.configure_key()
        os.environ["CHATWOOT_BASE_URL"] = "http://127.0.0.1:3000"
        os.environ["CHATWOOT_ACCOUNT_ID"] = "1"
        os.environ["CHATWOOT_API_TOKEN"] = "test-chatwoot-token"
        os.environ["CUSTOMER_SERVICE_HUMAN_SLA_SECONDS"] = "300"
        return headers

    def create_dingtalk_human_item(self) -> str:
        interaction = SQLiteDingTalkState(
            self.dingtalk_database_path
        ).record_interaction(
            message=DingTalkInboundMessage(
                message_id="dingtalk-message-1",
                sender_id="private-staff-id",
                conversation_id="private-conversation-id",
                text="跨城市调店后原门店分红怎么处理？",
                channel="direct",
            ),
            answer=EnterpriseAnswer(
                domain="shareholder",
                action="handoff",
                text="现有知识不足，请人工核实。",
                reason="knowledge_gap",
            ),
        )
        return interaction.ticket_code

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

    def test_ui_config_is_protected_brand_aware_and_secret_free(self) -> None:
        headers = self.configure_handoff()
        os.environ["ENTERPRISE_CONFIG_PATH"] = "config/enterprise.example.json"
        os.environ["CHATWOOT_PUBLIC_BASE_URL"] = "https://support.example.com"
        os.environ["DIFY_CONSOLE_PUBLIC_URL"] = "https://dify-admin.example.com"

        missing = self.client.get("/v1/operations/ui-config")
        response = self.client.get("/v1/operations/ui-config", headers=headers)

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(
            response.json(),
            {
                "company_name": "星河优选",
                "assistant_name": "星河优选 AI 客服",
                "chatwoot_url": "https://support.example.com",
                "dify_console_url": "https://dify-admin.example.com",
            },
        )
        self.assertNotIn("correct-secret", response.text)
        self.assertNotIn("test-chatwoot-token", response.text)

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

    def test_dingtalk_human_review_is_protected_private_and_owner_safe(self) -> None:
        ticket_code = self.create_dingtalk_human_item()
        headers = self.configure_key()

        unauthorized = self.client.get("/v1/operations/dingtalk-interactions")
        listed = self.client.get(
            "/v1/operations/dingtalk-interactions",
            headers=headers,
        )
        claimed = self.client.patch(
            f"/v1/operations/dingtalk-interactions/{ticket_code}/claim",
            headers=headers,
            json={"owner": "小草"},
        )
        conflicting_claim = self.client.patch(
            f"/v1/operations/dingtalk-interactions/{ticket_code}/claim",
            headers=headers,
            json={"owner": "然哥"},
        )
        wrong_owner_resolution = self.client.patch(
            f"/v1/operations/dingtalk-interactions/{ticket_code}/resolve",
            headers=headers,
            json={
                "owner": "然哥",
                "final_answer": "错误领取人不能处理。",
                "reviewed_by": "稳哥",
                "knowledge_status": "approved",
            },
        )
        resolved = self.client.patch(
            f"/v1/operations/dingtalk-interactions/{ticket_code}/resolve",
            headers=headers,
            json={
                "owner": "小草",
                "final_answer": "该问题需按具体门店协议由股东事务负责人核实。",
                "reviewed_by": "稳哥",
                "knowledge_status": "not_reusable",
            },
        )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 1)
        item = listed.json()["items"][0]
        self.assertEqual(item["ticket_code"], ticket_code)
        self.assertNotIn("message_id", item)
        self.assertNotIn("sender_id", item)
        self.assertNotIn("conversation_id", item)
        self.assertNotIn("private-staff-id", listed.text)
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.json()["owner"], "小草")
        self.assertEqual(conflicting_claim.status_code, 409)
        self.assertEqual(wrong_owner_resolution.status_code, 409)
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["status"], "resolved")
        self.assertEqual(resolved.json()["knowledge_status"], "not_reusable")

    def test_dingtalk_human_review_rejects_invalid_payloads_and_missing_items(
        self,
    ) -> None:
        headers = self.configure_key()

        invalid_claim = self.client.patch(
            "/v1/operations/dingtalk-interactions/CS-20260727-000001/claim",
            headers=headers,
            json={"owner": "", "unexpected": True},
        )
        invalid_resolution = self.client.patch(
            "/v1/operations/dingtalk-interactions/CS-20260727-000001/resolve",
            headers=headers,
            json={
                "owner": "小草",
                "final_answer": "",
                "reviewed_by": "",
                "knowledge_status": "pending",
            },
        )
        missing = self.client.patch(
            "/v1/operations/dingtalk-interactions/CS-20260727-999999/claim",
            headers=headers,
            json={"owner": "小草"},
        )

        self.assertEqual(invalid_claim.status_code, 422)
        self.assertEqual(invalid_resolution.status_code, 422)
        self.assertEqual(missing.status_code, 404)

    def test_queue_health_is_protected_and_exposes_no_job_content(self) -> None:
        headers = self.configure_key()
        store = SQLiteJobStore(self.database_path)
        store.enqueue(
            IncomingMessage(
                message_id=701,
                account_id=1,
                conversation_id=70,
                sender_id=9,
                content="private customer question",
                labels=(),
            ),
            now=1_000,
        )
        claimed = store.claim_next("worker-a", now=1_001, lease_seconds=300)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        store.fail(
            claimed.id,
            resume_stage="generating",
            error_code="dify_auth_error",
            now=1_002,
        )
        store.record_heartbeat("worker-a", now=1_010)
        store.record_dependency_status(
            "dify", ok=False, error_code="dify_auth_error", checked_at=1_011
        )
        store.record_dependency_status(
            "chatwoot", ok=True, error_code=None, checked_at=1_012
        )

        unauthorized = self.client.get("/v1/operations/queue")
        with patch("app.operations_api.current_timestamp", return_value=1_020):
            response = self.client.get("/v1/operations/queue", headers=headers)

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queue"]["failed"], 1)
        self.assertIn("dify", response.json()["dependencies"])
        self.assertIn("chatwoot", response.json()["dependencies"])
        self.assertNotIn("private customer question", response.text)

    def test_human_conversation_list_is_protected_private_and_classified(self) -> None:
        headers = self.configure_handoff()
        SQLiteJobStore(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executemany(
                """
                INSERT INTO conversation_states (
                    conversation_id, dify_conversation_id, mode,
                    handoff_reason, human_since,
                    last_customer_message_id, last_customer_message_at,
                    last_human_reply_message_id, last_human_reply_at,
                    last_fallback_at
                ) VALUES (?, NULL, 'human', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (41, "explicit_human_request", 1_000, 101, 1_161, None, None, None),
                    (42, "knowledge_gap", 1_000, 102, 1_160, None, None, None),
                    (43, "complaint_or_legal", 1_000, 103, 1_100, None, None, 1_399),
                    (44, "explicit_human_request", 1_000, 104, 1_100, 105, 1_100, None),
                ),
            )
            connection.execute(
                """
                INSERT INTO inbound_jobs (
                    message_id, account_id, conversation_id, sender_id,
                    content, labels_json, stage, resume_stage,
                    outgoing_source_id, message_created_at,
                    available_at, created_at, updated_at
                ) VALUES (
                    900, 1, 43, 9, 'private customer question', '[]',
                    'failed', 'generating', 'ai-cs:900:reply',
                    1100, 1100, 1100, 1100
                )
                """
            )
            connection.commit()

        unauthorized = self.client.get("/v1/operations/human-conversations")
        with patch("app.operations_api.current_timestamp", return_value=1_400):
            response = self.client.get(
                "/v1/operations/human-conversations",
                headers=headers,
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200)
        items = {item["conversation_id"]: item for item in response.json()["items"]}
        self.assertEqual(
            (items[41]["waiting_seconds"], items[41]["sla_status"]),
            (239, "normal"),
        )
        self.assertEqual(
            (items[42]["waiting_seconds"], items[42]["sla_status"]),
            (240, "due_soon"),
        )
        self.assertEqual(
            (items[43]["waiting_seconds"], items[43]["sla_status"]),
            (300, "overdue"),
        )
        self.assertEqual(
            (items[44]["waiting_seconds"], items[44]["sla_status"]),
            (0, "normal"),
        )
        self.assertTrue(items[43]["has_blocking_job"])
        self.assertEqual(items[43]["last_fallback_at"], 1_399)
        self.assertNotIn("last_customer_message_id", response.text)
        self.assertNotIn("private customer question", response.text)
        self.assertNotIn("content", response.text)

    def test_handoff_routes_authenticate_before_loading_control_settings(self) -> None:
        self.configure_key()

        with patch(
            "app.operations_api.HandoffOperationsSettings.from_env",
            side_effect=AssertionError("must authenticate first"),
        ):
            response = self.client.get("/v1/operations/human-conversations")

        self.assertEqual(response.status_code, 401)

    def test_handoff_routes_return_503_when_control_is_not_configured(self) -> None:
        headers = self.configure_key()

        response = self.client.get(
            "/v1/operations/human-conversations",
            headers=headers,
        )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("CHATWOOT_API_TOKEN", response.text)

    def test_handoff_control_uses_public_chatwoot_url_for_dashboard_links(self) -> None:
        settings = HandoffOperationsSettings(
            chatwoot_base_url="http://host.docker.internal:3000",
            chatwoot_public_base_url="http://127.0.0.1:3000",
            chatwoot_account_id=1,
            chatwoot_api_token="test-chatwoot-token",
            database_path=self.database_path,
            human_sla_seconds=300,
        )

        with patch("app.operations_api.ChatwootClient") as chatwoot, patch(
            "app.operations_api.HandoffControlService"
        ) as control:
            result = get_handoff_control_service(settings)

        self.assertIs(result, control.return_value)
        chatwoot.assert_called_once_with(
            base_url="http://host.docker.internal:3000",
            api_token="test-chatwoot-token",
            timeout=15,
        )
        self.assertEqual(
            control.call_args.kwargs["base_url"],
            "http://127.0.0.1:3000",
        )

    def test_resume_ai_api_maps_success_idempotency_and_failures(self) -> None:
        headers = self.configure_handoff()
        cases = (
            (ResumeResult("resumed"), None, 200, "resumed"),
            (ResumeResult("already_ai"), None, 200, "already_ai"),
            (None, HandoffNotFoundError(), 404, None),
            (None, HandoffConflictError(), 409, None),
            (None, HandoffRemoteError("chatwoot_server_error"), 502, None),
            (None, HandoffStorageError(), 503, None),
        )

        for result, error, expected_status, expected_body in cases:
            with self.subTest(expected_status=expected_status, error=error), patch(
                "app.operations_api.get_handoff_control_service"
            ) as factory:
                if error is None:
                    factory.return_value.resume_ai.return_value = result
                else:
                    factory.return_value.resume_ai.side_effect = error
                response = self.client.post(
                    "/v1/operations/conversations/42/resume-ai",
                    headers=headers,
                )
                self.assertEqual(response.status_code, expected_status)
                if expected_body is not None:
                    self.assertEqual(response.json()["status"], expected_body)
                self.assertNotIn("test-chatwoot-token", response.text)

        invalid = self.client.post(
            "/v1/operations/conversations/0/resume-ai",
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 422)

    def test_readiness_requires_writable_database_and_fresh_worker_heartbeat(self) -> None:
        from app.main import app as main_app, get_job_store

        ready_environment = {
            "DIFY_BASE_URL": "http://127.0.0.1:8081",
            "DIFY_APP_API_KEY": "test-dify-key",
            "CHATWOOT_BASE_URL": "http://127.0.0.1:3000",
            "CHATWOOT_ACCOUNT_ID": "1",
            "CHATWOOT_API_TOKEN": "test-chatwoot-token",
            "CHATWOOT_WEBHOOK_SECRET": "test-webhook-secret",
            "CUSTOMER_SERVICE_DB_PATH": str(self.database_path),
        }
        get_job_store.cache_clear()
        self.addCleanup(get_job_store.cache_clear)
        store = SQLiteJobStore(self.database_path)
        store.record_heartbeat("worker-a", now=1_000)

        with patch.dict(os.environ, ready_environment, clear=False), patch(
            "app.main.current_timestamp", return_value=1_020
        ), patch(
            "app.main.DifyChatClient",
            side_effect=AssertionError("readiness must not call Dify"),
        ), patch(
            "app.main.ChatwootClient",
            side_effect=AssertionError("readiness must not call Chatwoot"),
        ), TestClient(main_app) as client:
            response = client.get("/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})

    def test_readiness_rejects_missing_and_stale_heartbeat(self) -> None:
        from app.main import app as main_app, get_job_store

        missing_path = Path(self.temp_dir.name) / "missing-heartbeat.db"
        environment = {
            "DIFY_BASE_URL": "http://127.0.0.1:8081",
            "DIFY_APP_API_KEY": "test-dify-key",
            "CHATWOOT_BASE_URL": "http://127.0.0.1:3000",
            "CHATWOOT_ACCOUNT_ID": "1",
            "CHATWOOT_API_TOKEN": "test-chatwoot-token",
            "CHATWOOT_WEBHOOK_SECRET": "test-webhook-secret",
            "CUSTOMER_SERVICE_DB_PATH": str(missing_path),
        }
        get_job_store.cache_clear()
        self.addCleanup(get_job_store.cache_clear)
        SQLiteJobStore(missing_path)

        with patch.dict(os.environ, environment, clear=False), patch(
            "app.main.current_timestamp", return_value=1_000
        ), TestClient(main_app) as client:
            missing = client.get("/ready")
            SQLiteJobStore(missing_path).record_heartbeat("worker-a", now=900)
            stale = client.get("/ready")

        self.assertEqual(missing.status_code, 503)
        self.assertEqual(missing.json(), {"status": "not_ready"})
        self.assertEqual(stale.status_code, 503)
        self.assertEqual(stale.json(), {"status": "not_ready"})

    def test_inbound_queue_defers_operations_store_initialization(self) -> None:
        from app.main import get_inbound_queue
        from app.operations_models import RecoveringOperationsRecorder

        get_inbound_queue.cache_clear()
        self.addCleanup(get_inbound_queue.cache_clear)

        with patch("app.main.OperationsStore") as operations_store:
            queue = get_inbound_queue(self.database_path)

        self.assertIsInstance(queue._operations, RecoveringOperationsRecorder)
        operations_store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
