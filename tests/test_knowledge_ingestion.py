import unittest

from app.dify_client import CreatedDocument
from app.knowledge_ingestion import (
    FileTooLargeError,
    KnowledgeIngestionService,
    validate_document_upload,
)


class FakeDifyClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create_document_by_text(
        self,
        dataset_id: str,
        name: str,
        text: str,
        **kwargs: object,
    ) -> CreatedDocument:
        self.calls.append(
            {
                "dataset_id": dataset_id,
                "name": name,
                "text": text,
                "indexing_technique": str(kwargs.get("indexing_technique")),
            }
        )
        return CreatedDocument(
            document_id="document-123",
            batch="batch-456",
            indexing_status="indexing",
        )


class KnowledgeIngestionServiceTests(unittest.TestCase):
    def test_ingest_converts_source_and_publishes_markdown(self) -> None:
        client = FakeDifyClient()
        parser_calls: list[tuple[bytes, str]] = []

        def parser(content: bytes, filename: str) -> str:
            parser_calls.append((content, filename))
            return "# 售后政策\n\n七天无理由退货。"

        service = KnowledgeIngestionService(
            dataset_id="dataset-123",
            dify_client=client,
            parser=parser,
            indexing_technique="economy",
        )

        result = service.ingest(b"%PDF-1.7 fixture", "售后政策.pdf")

        self.assertEqual(parser_calls, [(b"%PDF-1.7 fixture", "售后政策.pdf")])
        self.assertEqual(
            client.calls,
            [
                {
                    "dataset_id": "dataset-123",
                    "name": "售后政策.pdf",
                    "text": "# 售后政策\n\n七天无理由退货。",
                    "indexing_technique": "economy",
                }
            ],
        )
        self.assertEqual(result.source_name, "售后政策.pdf")
        self.assertEqual(result.markdown_length, len("# 售后政策\n\n七天无理由退货。"))
        self.assertEqual(result.document_id, "document-123")
        self.assertEqual(result.batch, "batch-456")
        self.assertEqual(result.indexing_status, "indexing")

    def test_ingest_rejects_oversized_file_before_parsing(self) -> None:
        client = FakeDifyClient()
        parser_called = False

        def parser(content: bytes, filename: str) -> str:
            nonlocal parser_called
            parser_called = True
            return "unused"

        service = KnowledgeIngestionService(
            dataset_id="dataset-123",
            dify_client=client,
            parser=parser,
            max_file_size=4,
        )

        with self.assertRaises(FileTooLargeError):
            service.ingest(b"12345", "large.pdf")

        self.assertFalse(parser_called)
        self.assertEqual(client.calls, [])

    def test_ingest_rejects_empty_parser_result(self) -> None:
        service = KnowledgeIngestionService(
            dataset_id="dataset-123",
            dify_client=FakeDifyClient(),
            parser=lambda content, filename: "  ",
        )

        with self.assertRaises(ValueError):
            service.ingest(b"content", "empty.pdf")

    def test_upload_validation_allows_supported_signatures(self) -> None:
        cases = (
            (b"%PDF-1.7\n", "policy.pdf"),
            (b"PK\x03\x04fixture", "policy.docx"),
            (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture", "policy.doc"),
            ("# 售后政策".encode("utf-8"), "policy.md"),
            (b"<!doctype html><html></html>", "policy.html"),
        )
        for content, filename in cases:
            with self.subTest(filename=filename):
                self.assertEqual(validate_document_upload(content, filename), filename)

    def test_upload_validation_rejects_unsupported_or_mismatched_content(self) -> None:
        cases = (
            (b"MZ executable", "malware.exe"),
            (b"not a pdf", "fake.pdf"),
            (b"not a zip", "fake.docx"),
            (b"\x00\x01binary", "fake.txt"),
            (b"plain text", "fake.html"),
            (b"content", "../policy.pdf"),
        )
        for content, filename in cases:
            with self.subTest(filename=filename), self.assertRaisesRegex(
                ValueError, "document|filename|type|content"
            ):
                validate_document_upload(content, filename)


if __name__ == "__main__":
    unittest.main()
