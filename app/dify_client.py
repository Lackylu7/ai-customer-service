import json
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class Response(Protocol):
    def __enter__(self) -> "Response": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


OpenUrl = Callable[..., Response]


class DifyAPIError(RuntimeError):
    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CreatedDocument:
    document_id: str
    batch: str
    indexing_status: str


class DifyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30,
        opener: OpenUrl = urlopen,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Dify base URL is required.")
        if not api_key.strip():
            raise ValueError("Dify API key is required.")

        normalized_base_url = base_url.rstrip("/")
        if normalized_base_url.endswith("/v1"):
            normalized_base_url = normalized_base_url[:-3]

        self._base_url = normalized_base_url
        self._api_key = api_key
        self._timeout = timeout
        self._opener = opener

    def create_document_by_text(
        self,
        dataset_id: str,
        name: str,
        text: str,
        *,
        indexing_technique: str = "high_quality",
        doc_form: str = "text_model",
        doc_language: str = "Chinese",
    ) -> CreatedDocument:
        if not dataset_id.strip():
            raise ValueError("Dify dataset ID is required.")
        if not name.strip():
            raise ValueError("Document name is required.")
        if not text.strip():
            raise ValueError("Document text is required.")

        dataset_path = quote(dataset_id, safe="")
        url = f"{self._base_url}/v1/datasets/{dataset_path}/document/create-by-text"
        payload = {
            "name": name,
            "text": text,
            "indexing_technique": indexing_technique,
            "doc_form": doc_form,
            "doc_language": doc_language,
            "process_rule": {"mode": "automatic"},
        }
        if indexing_technique == "economy":
            payload["retrieval_model"] = {
                "search_method": "keyword_search",
                "reranking_enable": False,
                "top_k": 3,
                "score_threshold_enabled": False,
            }
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )

        try:
            with self._opener(request, timeout=self._timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise DifyAPIError(exc.code, self._read_error_message(exc)) from exc
        except URLError as exc:
            raise DifyAPIError(None, f"Dify API is unreachable: {exc.reason}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DifyAPIError(None, "Dify API returned an invalid JSON response.") from exc

        document = result.get("document")
        if not isinstance(document, dict):
            raise DifyAPIError(None, "Dify API response did not include a document.")

        document_id = document.get("id")
        batch = result.get("batch")
        if not isinstance(document_id, str) or not isinstance(batch, str):
            raise DifyAPIError(None, "Dify API response did not include document identifiers.")

        indexing_status = document.get("indexing_status", "indexing")
        return CreatedDocument(
            document_id=document_id,
            batch=batch,
            indexing_status=str(indexing_status),
        )

    @staticmethod
    def _read_error_message(error: HTTPError) -> str:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"Dify API returned HTTP {error.code}."

        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            return f"Dify API returned HTTP {error.code}."
        return message[:500]
