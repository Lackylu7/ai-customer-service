import io
import json
import unittest
from urllib.error import HTTPError, URLError

from app.dify_chat_client import DifyChatAPIError, DifyChatClient


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    @classmethod
    def from_json(cls, payload: object) -> "FakeResponse":
        return cls(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class DifyChatClientTests(unittest.TestCase):
    def test_send_message_posts_utf8_payload_and_parses_response(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse.from_json(
                {
                    "answer": "现货商品通常会在付款后 48 小时内发出。",
                    "conversation_id": "conversation-123",
                    "metadata": {
                        "retriever_resources": [
                            {"document_name": "customer-service-faq.md"}
                        ]
                    },
                }
            )

        client = DifyChatClient(
            base_url="http://127.0.0.1:8081/v1/",
            api_key="app-secret",
            timeout=12,
            opener=opener,
        )

        result = client.send_message(
            query="现货多久发货？",
            user="chatwoot:12",
        )

        request = captured["request"]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8081/v1/chat-messages",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer app-secret")
        self.assertEqual(
            request.get_header("Content-type"),
            "application/json; charset=utf-8",
        )
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "inputs": {},
                "query": "现货多久发货？",
                "response_mode": "blocking",
                "conversation_id": "",
                "user": "chatwoot:12",
            },
        )
        self.assertEqual(
            result.answer,
            "现货商品通常会在付款后 48 小时内发出。",
        )
        self.assertEqual(result.conversation_id, "conversation-123")
        self.assertEqual(len(result.retriever_resources), 1)

    def test_send_message_includes_existing_conversation_id(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            return FakeResponse.from_json(
                {
                    "answer": "可以继续追问。",
                    "conversation_id": "conversation-123",
                    "metadata": {},
                }
            )

        client = DifyChatClient(
            base_url="http://127.0.0.1:8081",
            api_key="app-secret",
            opener=opener,
        )
        client.send_message(
            query="那退货呢？",
            user="chatwoot:12",
            conversation_id="conversation-123",
        )

        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(payload["conversation_id"], "conversation-123")

    def test_remote_errors_do_not_leak_api_key(self) -> None:
        def http_error(request: object, timeout: float) -> FakeResponse:
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"message":"invalid token"}'),
            )

        client = DifyChatClient(
            base_url="http://127.0.0.1:8081",
            api_key="do-not-leak-this-app-key",
            opener=http_error,
        )
        with self.assertRaises(DifyChatAPIError) as raised:
            client.send_message(query="问题", user="chatwoot:12")

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("invalid token", str(raised.exception))
        self.assertNotIn("do-not-leak-this-app-key", str(raised.exception))

        def url_error(request: object, timeout: float) -> FakeResponse:
            raise URLError("connection refused")

        client = DifyChatClient(
            base_url="http://127.0.0.1:8081",
            api_key="do-not-leak-this-app-key",
            opener=url_error,
        )
        with self.assertRaises(DifyChatAPIError) as unreachable:
            client.send_message(query="问题", user="chatwoot:12")
        self.assertIsNone(unreachable.exception.status_code)
        self.assertNotIn("do-not-leak-this-app-key", str(unreachable.exception))

    def test_rejects_invalid_json_and_empty_answer(self) -> None:
        responses = iter(
            [
                FakeResponse(b"not-json"),
                FakeResponse.from_json(
                    {"answer": "  ", "conversation_id": "conversation-123"}
                ),
            ]
        )

        def opener(request: object, timeout: float) -> FakeResponse:
            return next(responses)

        client = DifyChatClient(
            base_url="http://127.0.0.1:8081",
            api_key="app-secret",
            opener=opener,
        )
        with self.assertRaises(DifyChatAPIError):
            client.send_message(query="问题", user="chatwoot:12")
        with self.assertRaises(DifyChatAPIError):
            client.send_message(query="问题", user="chatwoot:12")


if __name__ == "__main__":
    unittest.main()
