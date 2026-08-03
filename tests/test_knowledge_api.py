import base64
import os
import unittest
from dataclasses import asdict
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.knowledge_ingestion import IngestedKnowledgeDocument
from app.main import app


class KnowledgeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.environment = {
            "OPERATIONS_API_KEY": "knowledge-admin-secret",
            "DIFY_DATASET_ID": "dataset-id",
            "DIFY_DATASET_API_KEY": "dataset-api-key",
            "DIFY_INDEXING_TECHNIQUE": "economy",
        }

    @staticmethod
    def conversion_payload(content: bytes, filename: str) -> dict[str, object]:
        return {
            "file_sources": [
                {
                    "base64_string": base64.b64encode(content).decode("ascii"),
                    "filename": filename,
                }
            ]
        }

    def test_privileged_document_routes_require_bearer_auth_before_work(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True), patch(
            "app.main.parse_document"
        ) as parser:
            for path, request in (
                (
                    "/v1/convert/source",
                    lambda headers: self.client.post(
                        path,
                        json=self.conversion_payload(b"%PDF-1.7\n", "policy.pdf"),
                        headers=headers,
                    ),
                ),
                (
                    "/v1/knowledge/documents",
                    lambda headers: self.client.post(
                        path,
                        files={"file": ("policy.pdf", b"%PDF-1.7\n", "application/pdf")},
                        headers=headers,
                    ),
                ),
            ):
                with self.subTest(path=path):
                    self.assertEqual(request({}).status_code, 401)
                    self.assertEqual(
                        request({"Authorization": "Bearer wrong-secret"}).status_code,
                        401,
                    )
            parser.assert_not_called()

    def test_missing_admin_key_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post(
                "/v1/convert/source",
                json=self.conversion_payload(b"%PDF-1.7\n", "policy.pdf"),
                headers={"Authorization": "Bearer any-value"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("any-value", response.text)

    def test_authenticated_conversion_validates_type_before_parsing(self) -> None:
        headers = {"Authorization": "Bearer knowledge-admin-secret"}
        with patch.dict(os.environ, self.environment, clear=True), patch(
            "app.main.parse_document", return_value="# Policy"
        ) as parser:
            invalid = self.client.post(
                "/v1/convert/source",
                json=self.conversion_payload(b"MZ", "malware.exe"),
                headers=headers,
            )
            valid = self.client.post(
                "/v1/convert/source",
                json=self.conversion_payload(b"%PDF-1.7\n", "policy.pdf"),
                headers=headers,
            )

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json()["document"]["md_content"], "# Policy")
        parser.assert_called_once_with(b"%PDF-1.7\n", "policy.pdf")

    def test_authenticated_ingestion_keeps_dify_credentials_server_side(self) -> None:
        headers = {"Authorization": "Bearer knowledge-admin-secret"}
        result = IngestedKnowledgeDocument(
            source_name="policy.pdf",
            markdown_length=10,
            document_id="document-id",
            batch="batch-id",
            indexing_status="indexing",
        )
        service = Mock()
        service.ingest.return_value = result
        with patch.dict(os.environ, self.environment, clear=True), patch(
            "app.main.KnowledgeIngestionService", return_value=service
        ):
            response = self.client.post(
                "/v1/knowledge/documents",
                files={"file": ("policy.pdf", b"%PDF-1.7\n", "application/pdf")},
                headers=headers,
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), asdict(result))
        self.assertNotIn("dataset-api-key", response.text)


if __name__ == "__main__":
    unittest.main()
