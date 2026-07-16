import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IncomingMessage:
    message_id: int
    account_id: int
    conversation_id: int
    sender_id: int
    content: str
    labels: tuple[str, ...]
    created_at: int = 0


@dataclass(frozen=True)
class HumanReply:
    message_id: int
    account_id: int
    conversation_id: int
    created_at: int


def verify_webhook_signature(
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    *,
    now: int | None = None,
    max_age_seconds: int = 300,
) -> bool:
    if not timestamp or not signature or not secret:
        return False
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False

    current = int(time.time()) if now is None else now
    if abs(current - signed_at) > max_age_seconds:
        return False

    message = timestamp.encode("ascii") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature)


def parse_incoming_message(
    payload: Mapping[str, Any],
    *,
    fallback_created_at: int = 0,
) -> IncomingMessage | None:
    if payload.get("event") != "message_created":
        return None
    if payload.get("message_type") != "incoming":
        return None
    if payload.get("private") is True or payload.get("is_private") is True:
        return None
    if payload.get("content_type") not in {None, "text"}:
        return None

    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None

    account = payload.get("account")
    conversation = payload.get("conversation")
    sender = payload.get("sender")
    if not isinstance(account, Mapping):
        return None
    if not isinstance(conversation, Mapping):
        return None
    if not isinstance(sender, Mapping):
        return None

    message_id = _positive_int(payload.get("id"))
    account_id = _positive_int(account.get("id"))
    conversation_id = _positive_int(conversation.get("id"))
    sender_id = _positive_int(sender.get("id"))
    created_at = _event_timestamp(payload.get("created_at"), fallback_created_at)
    if None in {message_id, account_id, conversation_id, sender_id, created_at}:
        return None

    raw_labels = conversation.get("labels", [])
    labels = (
        tuple(label for label in raw_labels if isinstance(label, str) and label.strip())
        if isinstance(raw_labels, list)
        else ()
    )
    return IncomingMessage(
        message_id=message_id,
        account_id=account_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        content=content.strip(),
        labels=labels,
        created_at=created_at,
    )


def parse_human_reply(
    payload: Mapping[str, Any],
    *,
    fallback_created_at: int,
) -> HumanReply | None:
    if payload.get("event") != "message_created":
        return None
    message_type = payload.get("message_type")
    if message_type != "outgoing" and message_type != 1:
        return None
    if payload.get("private") is True or payload.get("is_private") is True:
        return None
    if payload.get("content_type") not in {None, "text"}:
        return None
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    source_id = payload.get("source_id")
    if isinstance(source_id, str) and source_id.startswith("ai-cs:"):
        return None

    account = payload.get("account")
    conversation = payload.get("conversation")
    sender = payload.get("sender")
    if not isinstance(account, Mapping):
        return None
    if not isinstance(conversation, Mapping):
        return None
    if not isinstance(sender, Mapping):
        return None
    sender_type = sender.get("type")
    if not isinstance(sender_type, str) or sender_type.casefold() != "user":
        return None

    message_id = _positive_int(payload.get("id"))
    account_id = _positive_int(account.get("id"))
    conversation_id = _positive_int(conversation.get("id"))
    created_at = _event_timestamp(payload.get("created_at"), fallback_created_at)
    if None in {message_id, account_id, conversation_id, created_at}:
        return None
    return HumanReply(
        message_id=message_id,
        account_id=account_id,
        conversation_id=conversation_id,
        created_at=created_at,
    )


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _event_timestamp(value: object, fallback: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(fallback, int) and not isinstance(fallback, bool) and fallback >= 0:
        return fallback
    return None
