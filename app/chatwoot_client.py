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


@dataclass(frozen=True)
class ChatwootConversation:
    id: int
    labels: tuple[str, ...]
    last_activity_at: int


class ChatwootAPIError(RuntimeError):
    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class ChatwootClient:
    _RETRYABLE_STATUS_CODES = {429, 502, 503, 504}

    def __init__(
        self,
        base_url: str,
        api_token: str,
        timeout: float = 15,
        opener: OpenUrl = urlopen,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url:
            raise ValueError("Chatwoot base URL is required.")
        if not api_token.strip():
            raise ValueError("Chatwoot API token is required.")
        if timeout <= 0:
            raise ValueError("Chatwoot timeout must be positive.")

        self._base_url = normalized_url
        self._api_token = api_token
        self._timeout = timeout
        self._opener = opener

    def send_outgoing_message(
        self,
        account_id: int,
        conversation_id: int,
        content: str,
        *,
        source_id: str | None = None,
    ) -> int:
        _require_positive_id(account_id, "Account ID")
        _require_positive_id(conversation_id, "Conversation ID")
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("Outgoing message content is required.")

        path = (
            f"/api/v1/accounts/{account_id}/conversations/"
            f"{conversation_id}/messages"
        )
        payload: dict[str, object] = {
            "content": normalized_content,
            "message_type": "outgoing",
            "private": False,
            "content_type": "text",
        }
        normalized_source_id = source_id.strip() if source_id is not None else ""
        if normalized_source_id:
            payload["source_id"] = normalized_source_id
        result = self._request_json("POST", path, payload)
        message_id = result.get("id") if isinstance(result, dict) else None
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise ChatwootAPIError(None, "Chatwoot returned no message ID.")
        return message_id

    def find_outgoing_message_by_source_id(
        self,
        account_id: int,
        conversation_id: int,
        source_id: str,
    ) -> int | None:
        _require_positive_id(account_id, "Account ID")
        _require_positive_id(conversation_id, "Conversation ID")
        normalized_source_id = source_id.strip()
        if not normalized_source_id:
            raise ValueError("Outgoing message source ID is required.")
        path = (
            f"/api/v1/accounts/{account_id}/conversations/"
            f"{conversation_id}/messages"
        )
        result = self._request_json("GET", path)
        raw_messages = result.get("payload") if isinstance(result, dict) else None
        if not isinstance(raw_messages, list):
            raise ChatwootAPIError(None, "Chatwoot returned invalid conversation messages.")

        for message in raw_messages:
            if not isinstance(message, dict):
                continue
            message_type = message.get("message_type")
            is_outgoing = message_type == "outgoing" or (
                isinstance(message_type, int)
                and not isinstance(message_type, bool)
                and message_type == 1
            )
            message_id = message.get("id")
            if (
                is_outgoing
                and message.get("private") is False
                and message.get("source_id") == normalized_source_id
                and isinstance(message_id, int)
                and not isinstance(message_id, bool)
                and message_id > 0
            ):
                return message_id
        return None

    def list_conversations_page(
        self,
        account_id: int,
        page: int,
    ) -> tuple[ChatwootConversation, ...]:
        _require_positive_id(account_id, "Account ID")
        _require_positive_id(page, "Page")
        path = (
            f"/api/v1/accounts/{account_id}/conversations"
            f"?status=all&sort_by=last_activity_at_desc&page={page}"
        )
        result = self._request_json("GET", path)
        data = result.get("data") if isinstance(result, dict) else None
        raw_conversations = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(raw_conversations, list):
            raise ChatwootAPIError(None, "Chatwoot returned invalid conversations.")

        conversations: list[ChatwootConversation] = []
        for raw in raw_conversations:
            if not isinstance(raw, dict):
                raise ChatwootAPIError(None, "Chatwoot returned invalid conversations.")
            conversation_id = raw.get("id")
            raw_labels = raw.get("labels")
            last_activity_at = raw.get("last_activity_at")
            if (
                not _is_positive_int(conversation_id)
                or not isinstance(raw_labels, list)
                or not isinstance(last_activity_at, int)
                or isinstance(last_activity_at, bool)
                or last_activity_at < 0
                or any(not isinstance(label, str) for label in raw_labels)
            ):
                raise ChatwootAPIError(None, "Chatwoot returned invalid conversations.")
            conversations.append(
                ChatwootConversation(
                    id=conversation_id,
                    labels=tuple(label.strip() for label in raw_labels if label.strip()),
                    last_activity_at=last_activity_at,
                )
            )
        return tuple(conversations)

    def list_conversation_messages(
        self,
        account_id: int,
        conversation_id: int,
    ) -> tuple[dict[str, object], ...]:
        _require_positive_id(account_id, "Account ID")
        _require_positive_id(conversation_id, "Conversation ID")
        path = (
            f"/api/v1/accounts/{account_id}/conversations/"
            f"{conversation_id}/messages"
        )
        result = self._request_json("GET", path)
        raw_messages = result.get("payload") if isinstance(result, dict) else None
        if not isinstance(raw_messages, list):
            raise ChatwootAPIError(None, "Chatwoot returned invalid conversation messages.")
        return tuple(message for message in raw_messages if isinstance(message, dict))

    def get_labels(self, account_id: int, conversation_id: int) -> tuple[str, ...]:
        _require_positive_id(account_id, "Account ID")
        _require_positive_id(conversation_id, "Conversation ID")
        path = (
            f"/api/v1/accounts/{account_id}/conversations/"
            f"{conversation_id}/labels"
        )
        result = self._request_json("GET", path)
        raw_labels = result.get("payload") if isinstance(result, dict) else None
        if not isinstance(raw_labels, list):
            raise ChatwootAPIError(None, "Chatwoot returned invalid conversation labels.")
        return tuple(
            label.strip()
            for label in raw_labels
            if isinstance(label, str) and label.strip()
        )

    def add_label_preserving_existing(
        self,
        account_id: int,
        conversation_id: int,
        label: str,
    ) -> tuple[str, ...]:
        normalized_label = label.strip()
        if not normalized_label:
            raise ValueError("Conversation label is required.")
        current = self.get_labels(account_id, conversation_id)
        labels = tuple(sorted(set(current) | {normalized_label}))
        path = (
            f"/api/v1/accounts/{account_id}/conversations/"
            f"{conversation_id}/labels"
        )
        self._request_json("POST", path, {"labels": list(labels)})
        return labels

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        data = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "api_access_token": self._api_token,
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )

        for attempt in range(2):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if attempt == 0 and exc.code in self._RETRYABLE_STATUS_CODES:
                    continue
                raise ChatwootAPIError(exc.code, self._read_error_message(exc)) from exc
            except URLError as exc:
                if attempt == 0:
                    continue
                raise ChatwootAPIError(
                    None,
                    f"Chatwoot API is unreachable: {exc.reason}",
                ) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ChatwootAPIError(None, "Chatwoot API returned invalid JSON.") from exc
        raise ChatwootAPIError(None, "Chatwoot API request failed.")

    @staticmethod
    def _read_error_message(error: HTTPError) -> str:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"Chatwoot API returned HTTP {error.code}."
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            return f"Chatwoot API returned HTTP {error.code}."
        return message[:500]


def _require_positive_id(value: int, label: str) -> None:
    if not _is_positive_int(value):
        raise ValueError(f"{label} must be a positive integer.")


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
