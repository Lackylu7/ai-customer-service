import io
import json
import unittest
from urllib.error import HTTPError

from app.chatwoot_client import ChatwootAPIError, ChatwootClient, ChatwootConversation


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class ChatwootClientTests(unittest.TestCase):
    def test_lists_conversations_and_dictionary_messages(self) -> None:
        requests: list[object] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            requests.append(request)
            if "?status=all" in request.full_url:
                return FakeResponse(
                    {
                        "data": {
                            "payload": [
                                {"id": 7, "labels": ["vip"], "last_activity_at": 1_000}
                            ]
                        }
                    }
                )
            return FakeResponse(
                {
                    "payload": [
                        {
                            "id": 501,
                            "message_type": 0,
                            "private": False,
                            "content_type": "text",
                            "content": "退款多久到帐？",
                            "created_at": 1_001,
                            "sender": {"id": 9},
                        },
                        "ignored-non-dictionary",
                    ]
                }
            )

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=opener,
        )

        conversations = client.list_conversations_page(2, 3)
        messages = client.list_conversation_messages(2, 7)

        self.assertEqual(
            conversations,
            (ChatwootConversation(id=7, labels=("vip",), last_activity_at=1_000),),
        )
        self.assertEqual(messages[0]["id"], 501)
        self.assertEqual(len(messages), 1)
        self.assertTrue(
            requests[0].full_url.endswith(
                "/api/v1/accounts/2/conversations?status=all&sort_by=last_activity_at_desc&page=3"
            )
        )

    def test_list_methods_reject_malformed_payload_shapes(self) -> None:
        responses = iter((FakeResponse({"data": {"payload": "bad"}}), FakeResponse({"payload": {}})))

        def opener(request: object, timeout: float) -> FakeResponse:
            return next(responses)

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=opener,
        )

        with self.assertRaises(ChatwootAPIError):
            client.list_conversations_page(1, 1)
        with self.assertRaises(ChatwootAPIError):
            client.list_conversation_messages(1, 7)

    def test_sends_public_outgoing_message(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"id": 501})

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000/",
            api_token="chatwoot-secret",
            timeout=8,
            opener=opener,
        )

        message_id = client.send_outgoing_message(
            account_id=1,
            conversation_id=42,
            content="现货会在 48 小时内发出。",
            source_id="ai-cs:101:reply",
        )

        request = captured["request"]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:3000/api/v1/accounts/1/conversations/42/messages",
        )
        self.assertEqual(
            request.get_header("Api_access_token"),
            "chatwoot-secret",
        )
        self.assertEqual(captured["timeout"], 8)
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "content": "现货会在 48 小时内发出。",
                "message_type": "outgoing",
                "private": False,
                "content_type": "text",
                "source_id": "ai-cs:101:reply",
            },
        )
        self.assertEqual(message_id, 501)

    def test_finds_matching_public_outgoing_message_by_source_id(self) -> None:
        def opener(request: object, timeout: float) -> FakeResponse:
            return FakeResponse(
                {
                    "payload": [
                        {
                            "id": 1,
                            "message_type": "outgoing",
                            "private": True,
                            "source_id": "ai-cs:101:reply",
                        },
                        {
                            "id": 2,
                            "message_type": "incoming",
                            "private": False,
                            "source_id": "ai-cs:101:reply",
                        },
                        {
                            "id": 3,
                            "message_type": 1,
                            "private": False,
                            "source_id": "ai-cs:other:reply",
                        },
                        {
                            "id": 9001,
                            "message_type": 1,
                            "private": False,
                            "source_id": "ai-cs:101:reply",
                        },
                    ]
                }
            )

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=opener,
        )

        self.assertEqual(
            client.find_outgoing_message_by_source_id(1, 42, "ai-cs:101:reply"),
            9001,
        )

    def test_add_label_preserves_existing_labels(self) -> None:
        requests: list[object] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            requests.append(request)
            if request.get_method() == "GET":
                return FakeResponse({"payload": ["vip", "售后"]})
            return FakeResponse({"payload": ["ai_handoff", "vip", "售后"]})

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=opener,
        )

        labels = client.add_label_preserving_existing(1, 42, "ai_handoff")

        expected_url = (
            "http://127.0.0.1:3000/api/v1/accounts/1/"
            "conversations/42/labels"
        )
        self.assertEqual([request.full_url for request in requests], [expected_url, expected_url])
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertEqual(requests[1].get_method(), "POST")
        self.assertEqual(
            json.loads(requests[1].data.decode("utf-8")),
            {"labels": ["ai_handoff", "vip", "售后"]},
        )
        self.assertEqual(labels, ("ai_handoff", "vip", "售后"))

    def test_remove_label_preserves_unrelated_labels(self) -> None:
        requests: list[object] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            requests.append(request)
            if request.get_method() == "GET":
                return FakeResponse({"payload": ["vip", "ai_handoff", "售后"]})
            return FakeResponse({"payload": ["vip", "售后"]})

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=opener,
        )

        labels = client.remove_label_preserving_existing(1, 42, "ai_handoff")

        self.assertEqual(labels, ("vip", "售后"))
        self.assertEqual([request.get_method() for request in requests], ["GET", "POST"])
        self.assertEqual(
            json.loads(requests[1].data.decode("utf-8")),
            {"labels": ["vip", "售后"]},
        )

    def test_remove_missing_label_does_not_post_unchanged_labels(self) -> None:
        requests: list[object] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            requests.append(request)
            return FakeResponse({"payload": ["vip", "售后"]})

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=opener,
        )

        labels = client.remove_label_preserving_existing(1, 42, "ai_handoff")

        self.assertEqual(labels, ("vip", "售后"))
        self.assertEqual([request.get_method() for request in requests], ["GET"])

    def test_latest_message_id_returns_maximum_valid_id(self) -> None:
        def opener(request: object, timeout: float) -> FakeResponse:
            return FakeResponse(
                {"payload": [{"id": 9}, {"id": 12}, {"id": "bad"}, {"id": True}]}
            )

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=opener,
        )

        self.assertEqual(client.latest_message_id(1, 42), 12)

    def test_latest_message_id_rejects_response_without_valid_id(self) -> None:
        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=lambda *args, **kwargs: FakeResponse(
                {"payload": [{"id": 0}, {"id": "bad"}]}
            ),
        )

        with self.assertRaises(ChatwootAPIError):
            client.latest_message_id(1, 42)

    def test_retries_transient_error_once(self) -> None:
        calls = 0

        def opener(request: object, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError(
                    request.full_url,
                    503,
                    "Unavailable",
                    hdrs=None,
                    fp=io.BytesIO(b'{"message":"try later"}'),
                )
            return FakeResponse({"id": 501})

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="chatwoot-secret",
            opener=opener,
        )

        self.assertEqual(client.send_outgoing_message(1, 42, "回复"), 501)
        self.assertEqual(calls, 2)

    def test_does_not_retry_auth_error_or_leak_token(self) -> None:
        calls = 0

        def opener(request: object, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"message":"invalid token"}'),
            )

        client = ChatwootClient(
            base_url="http://127.0.0.1:3000",
            api_token="do-not-leak-chatwoot-token",
            opener=opener,
        )

        with self.assertRaises(ChatwootAPIError) as raised:
            client.send_outgoing_message(1, 42, "回复")

        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("do-not-leak-chatwoot-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
