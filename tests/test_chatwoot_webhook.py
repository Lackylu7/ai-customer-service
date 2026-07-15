import hashlib
import hmac
import json
import unittest

from app.chatwoot_webhook import (
    WebhookAuthenticationError,
    WebhookPayloadError,
    process_chatwoot_webhook,
)
from app.customer_service import HandlingResult


class FakeCustomerService:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def handle(self, message: object) -> HandlingResult:
        self.messages.append(message)
        return HandlingResult(status="replied")


def payload(message_type: str = "incoming") -> dict[str, object]:
    return {
        "event": "message_created",
        "id": 101,
        "content": "现货多久发货？",
        "message_type": message_type,
        "content_type": "text",
        "private": False,
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
        self.service = FakeCustomerService()

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
            service=self.service,
            webhook_secret="webhook-secret",
            expected_account_id=1,
            now=1720958401,
        )

    def test_invalid_signature_is_rejected_before_json_parsing(self) -> None:
        with self.assertRaises(WebhookAuthenticationError):
            self.process(b"not-json", "sha256=invalid")
        self.assertEqual(self.service.messages, [])

    def test_malformed_json_is_rejected(self) -> None:
        raw_body = b"not-json"
        digest = hmac.new(
            b"webhook-secret",
            b"1720958400." + raw_body,
            hashlib.sha256,
        ).hexdigest()

        with self.assertRaises(WebhookPayloadError):
            self.process(raw_body, f"sha256={digest}")

    def test_ignored_event_does_not_call_customer_service(self) -> None:
        raw_body, signature = encode_and_sign(payload("outgoing"))

        result = self.process(raw_body, signature)

        self.assertEqual(result.status, "ignored")
        self.assertEqual(self.service.messages, [])

    def test_valid_event_calls_customer_service_once(self) -> None:
        raw_body, signature = encode_and_sign(payload())

        result = self.process(raw_body, signature)

        self.assertEqual(result.status, "replied")
        self.assertEqual(len(self.service.messages), 1)
        self.assertEqual(self.service.messages[0].conversation_id, 42)

    def test_event_for_other_account_is_ignored(self) -> None:
        other_account = payload()
        other_account["account"] = {"id": 2}
        raw_body, signature = encode_and_sign(other_account)

        result = self.process(raw_body, signature)

        self.assertEqual(result.status, "ignored")
        self.assertEqual(self.service.messages, [])


if __name__ == "__main__":
    unittest.main()
