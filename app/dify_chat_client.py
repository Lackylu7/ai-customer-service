import json
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class Response(Protocol):
    def __enter__(self) -> "Response": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


OpenUrl = Callable[..., Response]


class DifyChatAPIError(RuntimeError):
    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DifyChatResponse:
    answer: str
    conversation_id: str
    retriever_resources: tuple[dict[str, object], ...]


class DifyChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30,
        opener: OpenUrl = urlopen,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url:
            raise ValueError("Dify base URL is required.")
        if not api_key.strip():
            raise ValueError("Dify app API key is required.")
        if timeout <= 0:
            raise ValueError("Dify timeout must be positive.")

        self._api_base_url = (
            normalized_url if normalized_url.endswith("/v1") else f"{normalized_url}/v1"
        )
        self._api_key = api_key
        self._timeout = timeout
        self._opener = opener

    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse:
        normalized_query = query.strip()
        normalized_user = user.strip()
        if not normalized_query:
            raise ValueError("Dify query is required.")
        if not normalized_user:
            raise ValueError("Dify user is required.")

        payload = {
            "inputs": {},
            "query": normalized_query,
            "response_mode": "blocking",
            "conversation_id": (conversation_id or "").strip(),
            "user": normalized_user,
        }
        request = Request(
            f"{self._api_base_url}/chat-messages",
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
            raise DifyChatAPIError(exc.code, self._read_error_message(exc)) from exc
        except URLError as exc:
            raise DifyChatAPIError(None, f"Dify chat API is unreachable: {exc.reason}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DifyChatAPIError(None, "Dify chat API returned invalid JSON.") from exc

        if not isinstance(result, dict):
            raise DifyChatAPIError(None, "Dify chat API returned an invalid response.")
        answer = result.get("answer")
        returned_conversation_id = result.get("conversation_id")
        if not isinstance(answer, str) or not answer.strip():
            raise DifyChatAPIError(None, "Dify chat API returned an empty answer.")
        if not isinstance(returned_conversation_id, str) or not returned_conversation_id.strip():
            raise DifyChatAPIError(None, "Dify chat API returned no conversation ID.")

        metadata = result.get("metadata")
        raw_resources = metadata.get("retriever_resources", []) if isinstance(metadata, dict) else []
        resources = (
            tuple(resource for resource in raw_resources if isinstance(resource, dict))
            if isinstance(raw_resources, list)
            else ()
        )
        return DifyChatResponse(
            answer=answer.strip(),
            conversation_id=returned_conversation_id.strip(),
            retriever_resources=resources,
        )

    @staticmethod
    def _read_error_message(error: HTTPError) -> str:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"Dify chat API returned HTTP {error.code}."
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            return f"Dify chat API returned HTTP {error.code}."
        return message[:500]
