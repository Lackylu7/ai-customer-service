import io
import json
import unittest
from urllib.error import HTTPError

from app.dify_client import DifyAPIError, DifyClient


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class DifyClientTests(unittest.TestCase):
    def test_create_document_by_text_sends_expected_request(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "document": {
                        "id": "document-123",
                        "indexing_status": "indexing",
                    },
                    "batch": "batch-456",
                }
            )

        client = DifyClient(
            base_url="http://127.0.0.1:8081/",
            api_key="dataset-secret",
            timeout=12,
            opener=opener,
        )

        result = client.create_document_by_text(
            dataset_id="dataset-123",
            name="售后政策.md",
            text="# 售后政策\n\n七天无理由退货。",
        )

        request = captured["request"]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8081/v1/datasets/dataset-123/document/create-by-text",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer dataset-secret")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertEqual(captured["timeout"], 12)

        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["name"], "售后政策.md")
        self.assertEqual(payload["text"], "# 售后政策\n\n七天无理由退货。")
        self.assertEqual(payload["indexing_technique"], "high_quality")
        self.assertEqual(payload["doc_form"], "text_model")
        self.assertEqual(payload["doc_language"], "Chinese")
        self.assertEqual(payload["process_rule"], {"mode": "automatic"})
        self.assertEqual(result.document_id, "document-123")
        self.assertEqual(result.batch, "batch-456")
        self.assertEqual(result.indexing_status, "indexing")

    def test_create_economy_document_configures_keyword_retrieval(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            return FakeResponse(
                {
                    "document": {"id": "document-123", "indexing_status": "waiting"},
                    "batch": "batch-456",
                }
            )

        client = DifyClient(
            base_url="http://127.0.0.1:8081",
            api_key="dataset-secret",
            opener=opener,
        )

        client.create_document_by_text(
            dataset_id="dataset-123",
            name="客服知识.md",
            text="发货与退货规则。",
            indexing_technique="economy",
        )

        request = captured["request"]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            payload["retrieval_model"],
            {
                "search_method": "keyword_search",
                "reranking_enable": False,
                "top_k": 3,
                "score_threshold_enabled": False,
            },
        )

    def test_api_error_does_not_leak_api_key(self) -> None:
        def opener(request: object, timeout: float) -> FakeResponse:
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"message":"invalid token"}'),
            )

        client = DifyClient(
            base_url="http://127.0.0.1:8081",
            api_key="do-not-leak-this-key",
            opener=opener,
        )

        with self.assertRaises(DifyAPIError) as raised:
            client.create_document_by_text(
                dataset_id="dataset-123",
                name="guide.md",
                text="content",
            )

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("invalid token", str(raised.exception))
        self.assertNotIn("do-not-leak-this-key", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
