import json
from dataclasses import dataclass
from typing import Literal, Protocol

from app.chatwoot_events import (
    IncomingMessage,
    parse_incoming_message,
    verify_webhook_signature,
)
from app.job_models import EnqueueResult


class WebhookAuthenticationError(ValueError):
    pass


class WebhookPayloadError(ValueError):
    pass


class IncomingMessageEnqueuer(Protocol):
    def enqueue(self, message: IncomingMessage) -> EnqueueResult: ...


@dataclass(frozen=True)
class WebhookProcessingResult:
    status: Literal["queued", "duplicate", "ignored"]


def process_chatwoot_webhook(
    *,
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    enqueuer: IncomingMessageEnqueuer,
    webhook_secret: str,
    expected_account_id: int,
    now: int | None = None,
) -> WebhookProcessingResult:
    if not verify_webhook_signature(
        raw_body,
        timestamp,
        signature,
        webhook_secret,
        now=now,
    ):
        raise WebhookAuthenticationError("Invalid Chatwoot webhook signature.")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookPayloadError("Invalid Chatwoot webhook payload.") from exc
    if not isinstance(payload, dict):
        raise WebhookPayloadError("Invalid Chatwoot webhook payload.")

    message = parse_incoming_message(payload)
    if message is None or message.account_id != expected_account_id:
        return WebhookProcessingResult(status="ignored")

    result = enqueuer.enqueue(message)
    return WebhookProcessingResult(status=result.status)
