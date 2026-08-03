from collections.abc import Mapping
from typing import Protocol

from app.dify_chat_client import DifyChatResponse


class AssistantBrain(Protocol):
    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse: ...


class ConfiguredAssistantBrain:
    """Select one configured answering backend for all assistant requests."""

    def __init__(
        self,
        *,
        active_backend: str,
        backends: Mapping[str, AssistantBrain],
    ) -> None:
        normalized_backend = active_backend.strip()
        if normalized_backend not in backends:
            raise ValueError(
                f"Active assistant backend {normalized_backend!r} is not configured."
            )
        self.active_backend = normalized_backend
        self._backend = backends[normalized_backend]

    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse:
        return self._backend.send_message(
            query=query,
            user=user,
            conversation_id=conversation_id,
        )
