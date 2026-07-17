import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)
from fastapi.testclient import TestClient

from app.chatwoot_events import HumanReply, IncomingMessage
from app.chatwoot_webhook import (
    WebhookAuthenticationError,
    WebhookPayloadError,
    process_chatwoot_webhook,
)
from app.job_models import EnqueueResult


class FakeInboundQueue:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []
        self.human_replies: list[HumanReply] = []

    def enqueue(self, message: IncomingMessage) -> EnqueueResult:
        self.messages.append(message)
        status = "queued" if len(self.messages) == 1 else "duplicate"
        return EnqueueResult(status=status, job_id=1 if status == "queued" else None)

    def record_human_reply(self, reply: HumanReply) -> None:
        self.human_replies.append(reply)


def payload(message_type: str = "incoming") -> dict[str, object]:
    return {
        "event": "message_created",
        "id": 101,
        "content": "现货多久发货？",
        "message_type": message_type,
        "content_type": "text",
        "private": False,
        "created_at": 1_720_958_390,
        "account": {"id": 1},
        "conversation": {"id": 42, "labels": []},
        "sender": {"id": 9, "type": "contact"},
    }


def encode_and_sign(
    value: object,
    *,
    timestamp: str = "1720958400",
    secret: str = "webhook-secret",
) -> tuple[bytes, str]:
    raw_body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return raw_body, f"sha256={digest}"


class ChatwootWebhookProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = FakeInboundQueue()

    def process(
        self,
        raw_body: bytes,
        signature: str,
        *,
        timestamp: str = "1720958400",
    ) -> object:
        return process_chatwoot_webhook(
            raw_body=raw_body,
            signature=signature,
            timestamp=timestamp,
            enqueuer=self.queue,
            webhook_secret="webhook-secret",
            expected_account_id=1,
            now=1720958401,
        )

    def test_invalid_signature_is_rejected_before_json_parsing(self) -> None:
        with self.assertRaises(WebhookAuthenticationError):
            self.process(b"not-json", "sha256=invalid")
        self.assertEqual(self.queue.messages, [])

    def test_malformed_json_is_rejected(self) -> None:
        raw_body = b"not-json"
        digest = hmac.new(
            b"webhook-secret",
            b"1720958400." + raw_body,
            hashlib.sha256,
        ).hexdigest()

        with self.assertRaises(WebhookPayloadError):
            self.process(raw_body, f"sha256={digest}")

    def test_ignored_event_does_not_enqueue(self) -> None:
        value = payload("outgoing")
        value["sender"] = {"id": 7, "type": "AgentBot"}
        raw_body, signature = encode_and_sign(value)

        result = self.process(raw_body, signature)

        self.assertEqual(result.status, "ignored")
        self.assertEqual(self.queue.messages, [])

    def test_public_user_outgoing_records_human_activity(self) -> None:
        value = payload("outgoing")
        value["id"] = 102
        value["sender"] = {"id": 7, "type": "User"}
        raw_body, signature = encode_and_sign(value)

        result = self.process(raw_body, signature)

        self.assertEqual(result.status, "human_activity")
        self.assertEqual(self.queue.messages, [])
        self.assertEqual(
            self.queue.human_replies,
            [
                HumanReply(
                    message_id=102,
                    account_id=1,
                    conversation_id=42,
                    created_at=1_720_958_390,
                )
            ],
        )

    def test_ai_outgoing_is_not_recorded_as_human_activity(self) -> None:
        value = payload("outgoing")
        value["sender"] = {"id": 7, "type": "User"}
        value["source_id"] = "ai-cs:101:human-fallback"
        raw_body, signature = encode_and_sign(value)

        result = self.process(raw_body, signature)

        self.assertEqual(result.status, "ignored")
        self.assertEqual(self.queue.human_replies, [])

    def test_valid_event_is_queued_once_and_then_reported_duplicate(self) -> None:
        raw_body, signature = encode_and_sign(payload())

        first = self.process(raw_body, signature)
        second = self.process(raw_body, signature)

        self.assertEqual((first.status, second.status), ("queued", "duplicate"))
        self.assertEqual(len(self.queue.messages), 2)
        self.assertEqual(self.queue.messages[0].conversation_id, 42)

    def test_event_for_other_account_is_ignored(self) -> None:
        other_account = payload()
        other_account["account"] = {"id": 2}
        raw_body, signature = encode_and_sign(other_account)

        result = self.process(raw_body, signature)

        self.assertEqual(result.status, "ignored")
        self.assertEqual(self.queue.messages, [])


class ChatwootWebhookRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.main import app

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.queue = FakeInboundQueue()
        self.environment = patch.dict(
            os.environ,
            {
                "DIFY_BASE_URL": "http://127.0.0.1:8081",
                "DIFY_APP_API_KEY": "test-dify-key",
                "CHATWOOT_BASE_URL": "http://127.0.0.1:3000",
                "CHATWOOT_ACCOUNT_ID": "1",
                "CHATWOOT_API_TOKEN": "test-chatwoot-token",
                "CHATWOOT_WEBHOOK_SECRET": "webhook-secret",
                "CUSTOMER_SERVICE_DB_PATH": str(Path(self.temp_dir.name) / "state.db"),
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_route_returns_202_without_constructing_remote_clients(self) -> None:
        timestamp = str(int(time.time()))
        raw_body, signature = encode_and_sign(payload(), timestamp=timestamp)

        with patch("app.main.get_inbound_queue", return_value=self.queue), patch(
            "app.main.get_customer_service",
            side_effect=AssertionError("synchronous service must not be used"),
        ), patch(
            "app.main.DifyChatClient",
            side_effect=AssertionError("Dify must not be used by webhook"),
        ), patch(
            "app.main.ChatwootClient",
            side_effect=AssertionError("Chatwoot must not be used by webhook"),
        ):
            started = time.perf_counter()
            response = self.client.post(
                "/v1/chatwoot/webhook",
                content=raw_body,
                headers={
                    "X-Chatwoot-Timestamp": timestamp,
                    "X-Chatwoot-Signature": signature,
                },
            )
            elapsed = time.perf_counter() - started

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "queued"})
        self.assertLess(elapsed, 1.0)
        self.assertEqual(
            self.queue.messages,
            [
                IncomingMessage(
                    message_id=101,
                    account_id=1,
                    conversation_id=42,
                    sender_id=9,
                    content="现货多久发货？",
                    labels=(),
                    created_at=1_720_958_390,
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
