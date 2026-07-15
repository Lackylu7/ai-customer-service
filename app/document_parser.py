import tempfile
from pathlib import Path

from docling.document_converter import DocumentConverter


class DocumentParsingError(RuntimeError):
    pass


converter = DocumentConverter()


def parse_document(content: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary_file:
            temporary_file.write(content)
            temporary_path = Path(temporary_file.name)

        result = converter.convert(temporary_path)
        return result.document.export_to_markdown()
    except Exception as exc:
        raise DocumentParsingError("Document conversion failed.") from exc
    finally:
        if "temporary_path" in locals():
            temporary_path.unlink(missing_ok=True)
