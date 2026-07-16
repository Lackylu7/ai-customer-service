import hashlib
import hmac
import unittest

from app.chatwoot_events import (
    HumanReply,
    IncomingMessage,
    parse_human_reply,
    parse_incoming_message,
    verify_webhook_signature,
)


def sign(body: bytes, timestamp: str, secret: str) -> str:
    message = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def incoming_payload() -> dict[str, object]:
    return {
        "event": "message_created",
        "id": 101,
        "content": "  现货下单后多久发货？  ",
        "message_type": "incoming",
        "content_type": "text",
        "private": False,
        "account": {"id": 1},
        "conversation": {"id": 42, "labels": ["vip", "售后"]},
        "sender": {"id": 9, "type": "contact"},
    }


class ChatwootWebhookSignatureTests(unittest.TestCase):
    def test_accepts_fresh_valid_signature(self) -> None:
        body = b'{"event":"message_created"}'
        timestamp = "1720958400"
        signature = sign(body, timestamp, "webhook-secret")

        self.assertTrue(
            verify_webhook_signature(
                body,
                timestamp,
                signature,
                "webhook-secret",
                now=1720958401,
            )
        )

    def test_rejects_stale_modified_or_incomplete_request(self) -> None:
        body = b'{"event":"message_created"}'
        fresh_timestamp = "1720958400"
        signature = sign(body, fresh_timestamp, "webhook-secret")

        self.assertFalse(
            verify_webhook_signature(
                body,
                "1720958000",
                sign(body, "1720958000", "webhook-secret"),
                "webhook-secret",
                now=1720958401,
            )
        )
        self.assertFalse(
            verify_webhook_signature(
                body + b" ",
                fresh_timestamp,
                signature,
                "webhook-secret",
                now=1720958401,
            )
        )
        self.assertFalse(
            verify_webhook_signature(body, None, signature, "webhook-secret")
        )
        self.assertFalse(
            verify_webhook_signature(body, "invalid", signature, "webhook-secret")
        )


class ChatwootEventParsingTests(unittest.TestCase):
    def test_parses_public_incoming_text_message(self) -> None:
        self.assertEqual(
            parse_incoming_message(incoming_payload()),
            IncomingMessage(
                message_id=101,
                account_id=1,
                conversation_id=42,
                sender_id=9,
                content="现货下单后多久发货？",
                labels=("vip", "售后"),
            ),
        )

    def test_parses_event_time_and_public_human_reply(self) -> None:
        incoming = incoming_payload()
        incoming["created_at"] = 1_720_958_390

        parsed_incoming = parse_incoming_message(incoming)

        self.assertIsNotNone(parsed_incoming)
        assert parsed_incoming is not None
        self.assertEqual(parsed_incoming.created_at, 1_720_958_390)

        outgoing = incoming_payload()
        outgoing.update(
            {
                "id": 102,
                "message_type": "outgoing",
                "created_at": 1_720_958_400,
                "sender": {"id": 7, "type": "User"},
            }
        )

        self.assertEqual(
            parse_human_reply(
                outgoing,
                fallback_created_at=1_720_958_401,
            ),
            HumanReply(
                message_id=102,
                account_id=1,
                conversation_id=42,
                created_at=1_720_958_400,
            ),
        )

    def test_ignores_ai_private_bot_and_non_text_outgoing_messages(self) -> None:
        base = incoming_payload()
        base.update(
            {
                "message_type": "outgoing",
                "created_at": 1_720_958_400,
                "sender": {"id": 7, "type": "User"},
            }
        )
        variants = []
        for changes in (
            {"source_id": "ai-cs:101:reply"},
            {"private": True},
            {"sender": {"id": 7, "type": "AgentBot"}},
            {"event": "conversation_updated"},
            {"content_type": "input_select"},
        ):
            value = dict(base)
            value.update(changes)
            variants.append(value)

        for value in variants:
            with self.subTest(payload=value):
                self.assertIsNone(
                    parse_human_reply(
                        value,
                        fallback_created_at=1_720_958_401,
                    )
                )

    def test_ignores_events_that_must_not_trigger_ai(self) -> None:
        variants = []
        for field, value in (
            ("event", "conversation_updated"),
            ("message_type", "outgoing"),
            ("content_type", "input_select"),
            ("content", "  "),
            ("private", True),
        ):
            payload = incoming_payload()
            payload[field] = value
            variants.append(payload)

        legacy_private = incoming_payload()
        legacy_private.pop("private")
        legacy_private["is_private"] = True
        variants.append(legacy_private)

        for payload in variants:
            with self.subTest(payload=payload):
                self.assertIsNone(parse_incoming_message(payload))

    def test_ignores_malformed_identifiers_and_labels(self) -> None:
        payload = incoming_payload()
        payload["conversation"] = {"id": "not-a-number", "labels": []}
        self.assertIsNone(parse_incoming_message(payload))

        payload = incoming_payload()
        payload["account"] = {}
        self.assertIsNone(parse_incoming_message(payload))

        payload = incoming_payload()
        payload["sender"] = {"type": "contact"}
        self.assertIsNone(parse_incoming_message(payload))


if __name__ == "__main__":
    unittest.main()
