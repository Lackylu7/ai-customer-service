from dataclasses import dataclass
from pathlib import Path
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


_PDF_EXTENSIONS = {".pdf"}
_OPEN_XML_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
_OLE_EXTENSIONS = {".doc", ".ppt", ".xls"}
_TEXT_EXTENSIONS = {".txt", ".md", ".csv"}
_HTML_EXTENSIONS = {".html", ".htm"}
SUPPORTED_DOCUMENT_EXTENSIONS = (
    _PDF_EXTENSIONS
    | _OPEN_XML_EXTENSIONS
    | _OLE_EXTENSIONS
    | _TEXT_EXTENSIONS
    | _HTML_EXTENSIONS
)


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
        if not content:
            raise ValueError("Source document is empty.")
        if len(content) > self.max_file_size:
            raise FileTooLargeError(
                f"Source document exceeds the {self.max_file_size}-byte limit."
            )
        source_name = validate_document_upload(content, filename)

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


def validate_document_upload(content: bytes, filename: str) -> str:
    if not isinstance(filename, str):
        raise ValueError("Source filename is required.")
    source_name = filename.strip()
    if (
        not source_name
        or len(source_name) > 255
        or "/" in source_name
        or "\\" in source_name
        or Path(source_name).name != source_name
    ):
        raise ValueError("Source filename is invalid.")
    extension = Path(source_name).suffix.lower()
    if extension not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise ValueError("Unsupported source document type.")
    if not content:
        raise ValueError("Source document is empty.")

    if extension in _PDF_EXTENSIONS and not content.startswith(b"%PDF-"):
        raise ValueError("Source document content does not match its PDF type.")
    if extension in _OPEN_XML_EXTENSIONS and not content.startswith(b"PK\x03\x04"):
        raise ValueError("Source document content does not match its Office type.")
    if extension in _OLE_EXTENSIONS and not content.startswith(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    ):
        raise ValueError("Source document content does not match its Office type.")
    if extension in _TEXT_EXTENSIONS:
        _decode_safe_text(content)
    if extension in _HTML_EXTENSIONS:
        decoded = _decode_safe_text(content).lstrip().casefold()
        if not (decoded.startswith("<!doctype html") or "<html" in decoded[:2048]):
            raise ValueError("Source document content does not match its HTML type.")
    return source_name


def _decode_safe_text(content: bytes) -> str:
    if b"\x00" in content:
        raise ValueError("Source document content is not valid text.")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Source document content must be UTF-8 text.") from exc
