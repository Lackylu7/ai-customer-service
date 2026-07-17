import json
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from app.chatwoot_events import (
    HumanReply,
    IncomingMessage,
    parse_human_reply,
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

    def record_human_reply(self, reply: HumanReply) -> None: ...


@dataclass(frozen=True)
class WebhookProcessingResult:
    status: Literal["queued", "duplicate", "human_activity", "ignored"]


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

    accepted_at = int(time.time()) if now is None else now
    message = parse_incoming_message(
        payload,
        fallback_created_at=accepted_at,
    )
    if message is not None and message.account_id == expected_account_id:
        result = enqueuer.enqueue(message)
        return WebhookProcessingResult(status=result.status)

    human_reply = parse_human_reply(
        payload,
        fallback_created_at=accepted_at,
    )
    if human_reply is not None and human_reply.account_id == expected_account_id:
        enqueuer.record_human_reply(human_reply)
        return WebhookProcessingResult(status="human_activity")

    return WebhookProcessingResult(status="ignored")
