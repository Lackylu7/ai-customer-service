from dataclasses import dataclass
from typing import Callable, Protocol

from app.dify_client import CreatedDocument


DocumentParser = Callable[[bytes, str], str]


class DifyDocumentPublisher(Protocol):
    def create_document_by_text(
        self,
        dataset_id: str,
        name: str,
        text: str,
        **kwargs: object,
    ) -> CreatedDocument: ...


class FileTooLargeError(ValueError):
    pass


@dataclass(frozen=True)
class IngestedKnowledgeDocument:
    source_name: str
    markdown_length: int
    document_id: str
    batch: str
    indexing_status: str


class KnowledgeIngestionService:
    def __init__(
        self,
        dataset_id: str,
        dify_client: DifyDocumentPublisher,
        parser: DocumentParser,
        max_file_size: int = 20 * 1024 * 1024,
        indexing_technique: str = "economy",
    ) -> None:
        if not dataset_id.strip():
            raise ValueError("Dify dataset ID is required.")
        if max_file_size <= 0:
            raise ValueError("Maximum file size must be positive.")
        if indexing_technique not in {"economy", "high_quality"}:
            raise ValueError("Unsupported Dify indexing technique.")

        self._dataset_id = dataset_id
        self._dify_client = dify_client
        self._parser = parser
        self._indexing_technique = indexing_technique
        self.max_file_size = max_file_size

    def ingest(self, content: bytes, filename: str) -> IngestedKnowledgeDocument:
        source_name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not source_name:
            raise ValueError("Source filename is required.")
        if not content:
            raise ValueError("Source document is empty.")
        if len(content) > self.max_file_size:
            raise FileTooLargeError(
                f"Source document exceeds the {self.max_file_size}-byte limit."
            )

        markdown = self._parser(content, source_name)
        if not markdown.strip():
            raise ValueError("Document parser returned empty content.")

        created = self._dify_client.create_document_by_text(
            dataset_id=self._dataset_id,
            name=source_name,
            text=markdown,
            indexing_technique=self._indexing_technique,
        )
        return IngestedKnowledgeDocument(
            source_name=source_name,
            markdown_length=len(markdown),
            document_id=created.document_id,
            batch=created.batch,
            indexing_status=created.indexing_status,
        )
