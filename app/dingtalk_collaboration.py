from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.dingtalk_adapter import DingTalkInteraction, DingTalkState


class CollaborationGroupError(RuntimeError):
    pass


@dataclass(frozen=True)
class CollaborationGroup:
    group_id: str
    name: str


@dataclass(frozen=True)
class CollaborationResult:
    group_id: str
    group_name: str
    reused: bool
    employee_message: str


@dataclass(frozen=True)
class CollaborationMessage:
    message_id: str
    sender_id: str
    sender_name: str
    content: str
    created_at: int


class CollaborationTranscriptGateway(Protocol):
    def list_messages(
        self,
        *,
        group_id: str,
        since: int,
    ) -> tuple[CollaborationMessage, ...]: ...


class CollaborationGroupGateway(Protocol):
    def create_group(
        self,
        *,
        name: str,
        member_user_ids: Sequence[str],
    ) -> CollaborationGroup: ...

    def add_bot(self, group_id: str) -> None: ...

    def send_text(self, group_id: str, text: str, *, title: str) -> None: ...


class DwsCollaborationGroupGateway:
    """Use an authenticated DWS profile for pilot group orchestration."""

    def __init__(
        self,
        *,
        robot_code: str,
        executable: str = "dws",
        profile: str | None = None,
        timeout: float = 30,
    ) -> None:
        self._robot_code = _require_text(robot_code, "robot_code")
        self._executable = _resolve_dws_executable(executable)
        self._profile = profile.strip() if profile and profile.strip() else None
        if timeout <= 0:
            raise ValueError("timeout must be positive.")
        self._timeout = timeout

    def create_group(
        self,
        *,
        name: str,
        member_user_ids: Sequence[str],
    ) -> CollaborationGroup:
        normalized_name = _require_text(name, "name")
        members = tuple(
            dict.fromkeys(
                _require_text(user_id, "member_user_id")
                for user_id in member_user_ids
            )
        )
        if len(members) < 2:
            raise ValueError("At least two human group members are required.")
        result = self._run(
            "chat",
            "group",
            "create",
            "--name",
            normalized_name,
            "--users",
            ",".join(members),
            "--type",
            "INTERNAL",
        )
        group_id = _find_text_value(
            result,
            ("openConversationId", "openconversation_id", "conversationId"),
        )
        if group_id is None:
            raise CollaborationGroupError(
                "DWS created a group but returned no conversation identifier."
            )
        return CollaborationGroup(group_id=group_id, name=normalized_name)

    def add_bot(self, group_id: str) -> None:
        self._run(
            "chat",
            "group",
            "members",
            "add-bot",
            "--id",
            _require_text(group_id, "group_id"),
            "--robot-code",
            self._robot_code,
        )

    def send_text(self, group_id: str, text: str, *, title: str) -> None:
        self._run(
            "chat",
            "message",
            "send-by-bot",
            "--robot-code",
            self._robot_code,
            "--group",
            _require_text(group_id, "group_id"),
            "--title",
            _require_text(title, "title"),
            "--text",
            _require_text(text, "text"),
        )

    def list_messages(
        self,
        *,
        group_id: str,
        since: int,
    ) -> tuple[CollaborationMessage, ...]:
        normalized_group_id = _require_text(group_id, "group_id")
        if since <= 0:
            raise ValueError("since must be a positive Unix timestamp.")
        boundary = since
        collected: dict[str, CollaborationMessage] = {}
        for _ in range(50):
            result = self._run(
                "chat",
                "message",
                "list",
                "--group",
                normalized_group_id,
                "--time",
                datetime.fromtimestamp(boundary).strftime("%Y-%m-%d %H:%M:%S"),
                "--direction",
                "newer",
                "--limit",
                "100",
            )
            page = _parse_messages(result)
            for message in page:
                collected[message.message_id] = message
            if not _find_bool_value(result, ("hasMore", "has_more")):
                break
            next_boundary = max(
                (message.created_at for message in page),
                default=boundary,
            )
            if next_boundary <= boundary:
                raise CollaborationGroupError(
                    "DWS message pagination did not advance."
                )
            boundary = next_boundary
        else:
            raise CollaborationGroupError(
                "DWS message history exceeded the pagination safety limit."
            )
        return tuple(
            sorted(
                collected.values(),
                key=lambda message: (message.created_at, message.message_id),
            )
        )

    def resolve_sender_aliases(
        self,
        user_ids: Sequence[str],
    ) -> dict[str, str]:
        """Map enterprise userIds to the openDingTalkIds used by history APIs."""
        normalized_user_ids = tuple(
            dict.fromkeys(_require_text(value, "user_id") for value in user_ids)
        )
        if not normalized_user_ids:
            return {}
        directory_result = self._run(
            "contact",
            "user",
            "get",
            "--ids",
            ",".join(normalized_user_ids),
        )
        aliases: dict[str, str] = {}
        for user_id in normalized_user_ids:
            user_name = _find_user_name_for_user(
                directory_result,
                user_id=user_id,
            )
            if user_name is None:
                raise CollaborationGroupError(
                    "DWS could not resolve the name for configured "
                    f"user {user_id}."
                )
            result = self._run(
                "contact",
                "user",
                "search",
                "--query",
                user_name,
            )
            open_id = _find_open_dingtalk_id_for_user(result, user_id=user_id)
            if open_id is None:
                raise CollaborationGroupError(
                    "DWS could not resolve the openDingTalkId for configured "
                    f"user {user_id}."
                )
            aliases[user_id] = open_id
        return aliases

    def _run(self, *arguments: str) -> dict[str, object]:
        command = [self._executable, *arguments, "--format", "json"]
        if self._profile is not None:
            command.extend(("--profile", self._profile))
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CollaborationGroupError(
                f"DWS group operation could not run: {exc}"
            ) from exc
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).strip()
            raise CollaborationGroupError(
                f"DWS group operation failed: {message[:500]}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CollaborationGroupError(
                "DWS group operation returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise CollaborationGroupError(
                "DWS group operation returned an invalid response."
            )
        return payload


class DingTalkCollaborationService:
    def __init__(
        self,
        *,
        state: DingTalkState,
        gateway: CollaborationGroupGateway,
        handler_user_ids_by_domain: Mapping[str, str],
        operator_user_ids: Sequence[str] = (),
        group_name_prefix: str = "内部服务协作",
    ) -> None:
        self._state = state
        self._gateway = gateway
        self._handler_user_ids_by_domain = dict(handler_user_ids_by_domain)
        self._operator_user_ids = tuple(
            dict.fromkeys(
                _require_text(user_id, "operator_user_id")
                for user_id in operator_user_ids
            )
        )
        self._group_name_prefix = _require_text(
            group_name_prefix,
            "group_name_prefix",
        )

    def ensure_group(self, interaction: DingTalkInteraction) -> CollaborationResult:
        if interaction.channel != "direct":
            raise CollaborationGroupError(
                "Nested collaboration groups are not created from group messages."
            )
        owner_id = self._handler_user_ids_by_domain.get(interaction.domain)
        if owner_id is None:
            raise CollaborationGroupError(
                f"No department owner is configured for {interaction.domain}."
            )

        current = self._state.get_interaction(interaction.ticket_code)
        if current is not None and current.collaboration_group_id is not None:
            return CollaborationResult(
                group_id=current.collaboration_group_id,
                group_name=current.collaboration_group_name or "协作群",
                reused=True,
                employee_message=(
                    f"这个问题的协作群“{current.collaboration_group_name or '协作群'}”"
                    "已经创建，"
                    "请直接在群内与负责人继续沟通。"
                ),
            )

        group = self._gateway.create_group(
            name=self._group_name(interaction),
            member_user_ids=tuple(
                dict.fromkeys(
                    (interaction.sender_id, owner_id, *self._operator_user_ids)
                )
            ),
        )
        self._gateway.add_bot(group.group_id)
        self._gateway.send_text(
            group.group_id,
            _opening_text(interaction),
            title=_opening_title(interaction),
        )
        attached = self._state.attach_collaboration_group(
            ticket_code=interaction.ticket_code,
            group_id=group.group_id,
            group_name=group.name,
        )
        if attached is None:
            raise CollaborationGroupError(
                "The collaboration group was created but could not be recorded."
            )
        return CollaborationResult(
            group_id=group.group_id,
            group_name=group.name,
            reused=False,
            employee_message=(
                f"这个问题需要负责人进一步确认，已为你创建协作群“{group.name}”。"
                "请直接在群内沟通，我们会跟进到问题解决。"
            ),
        )

    def _group_name(self, interaction: DingTalkInteraction) -> str:
        return (
            f"{self._group_name_prefix}-"
            f"{_domain_label(interaction.domain)}-{interaction.ticket_code}"
        )[:64]


def _opening_title(interaction: DingTalkInteraction) -> str:
    question = _question_headline(interaction.question)
    return f"{_domain_label(interaction.domain)}待确认：{_shorten(question, 40)}"


def _opening_text(interaction: DingTalkInteraction) -> str:
    return (
        f"## {_domain_label(interaction.domain)}｜需要负责人协助\n\n"
        f"**问题概览：** {_shorten(_question_headline(interaction.question), 100)}\n\n"
        "### 完整问题链\n"
        f"{_question_context(interaction)}\n\n"
        "### 当前需要\n"
        f"{_handoff_reason(interaction)}。请负责人直接在群里与员工沟通并给出明确结论。"
        "机器人会记录本群结论，用于结案和后续知识完善。\n\n"
        f"`工单：{interaction.ticket_code}`"
    )

def _handoff_reason(interaction: DingTalkInteraction) -> str:
    if interaction.feedback == "unresolved":
        return "员工反馈原答复没有解决问题"
    return _reason_label(interaction.reason)


def _question_headline(question: str) -> str:
    normalized_lines = [
        " ".join(line.split()) for line in question.splitlines() if line.strip()
    ]
    if not normalized_lines:
        return "待确认问题"
    first = normalized_lines[0]
    for prefix in ("员工最初提问：", "原始问题：", "员工问题："):
        if first.startswith(prefix):
            return first[len(prefix) :].strip() or "待确认问题"
    return first


def _question_context(interaction: DingTalkInteraction) -> str:
    question_lines = [
        " ".join(line.split())
        for line in interaction.question.splitlines()
        if line.strip()
    ]
    has_structured_chain = any(
        line.startswith(("员工最初提问：", "机器人追问：", "员工补充："))
        for line in question_lines
    )
    if has_structured_chain:
        lines = [f"- {_shorten(line, 700)}" for line in question_lines]
    else:
        lines = [f"- 员工提问：{_shorten(interaction.question, 1400)}"]

    if interaction.feedback == "unresolved":
        if interaction.answer.strip():
            lines.append(
                f"- 机器人此前答复：{_shorten(interaction.answer, 1000)}"
            )
        lines.append("- 员工反馈：上述答复没有解决问题")
    return "\n".join(lines)


def _find_text_value(
    value: object,
    keys: Sequence[str],
) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for nested in value.values():
            found = _find_text_value(nested, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_text_value(nested, keys)
            if found is not None:
                return found
    return None


def _find_bool_value(value: object, keys: Sequence[str]) -> bool:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, bool):
                return candidate
        return any(_find_bool_value(nested, keys) for nested in value.values())
    if isinstance(value, list):
        return any(_find_bool_value(nested, keys) for nested in value)
    return False


def _find_open_dingtalk_id_for_user(
    value: object,
    *,
    user_id: str,
) -> str | None:
    if isinstance(value, dict):
        candidate_user_id = value.get("userId") or value.get("user_id")
        if candidate_user_id == user_id:
            open_id = value.get("openDingTalkId") or value.get("open_dingtalk_id")
            if isinstance(open_id, str) and open_id.strip():
                return open_id.strip()
        for nested in value.values():
            found = _find_open_dingtalk_id_for_user(nested, user_id=user_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_open_dingtalk_id_for_user(nested, user_id=user_id)
            if found is not None:
                return found
    return None


def _find_user_name_for_user(
    value: object,
    *,
    user_id: str,
) -> str | None:
    if isinstance(value, dict):
        candidate_user_id = (
            value.get("userId")
            or value.get("user_id")
            or value.get("orgUserId")
            or value.get("org_user_id")
        )
        if candidate_user_id == user_id:
            user_name = (
                value.get("orgUserName")
                or value.get("org_user_name")
                or value.get("name")
                or value.get("nick")
            )
            if isinstance(user_name, str) and user_name.strip():
                return user_name.strip()
        for nested in value.values():
            found = _find_user_name_for_user(nested, user_id=user_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_user_name_for_user(nested, user_id=user_id)
            if found is not None:
                return found
    return None


def _parse_messages(payload: Mapping[str, object]) -> tuple[CollaborationMessage, ...]:
    raw_messages = _find_message_list(payload)
    messages: list[CollaborationMessage] = []
    for raw in raw_messages:
        message_id = _find_text_value(
            raw,
            (
                "openMessageId",
                "openMsgId",
                "messageId",
                "msgId",
                "message_id",
                "id",
            ),
        )
        content = _message_content(raw)
        created_at = _message_timestamp(raw)
        if message_id is None or content is None or created_at is None:
            continue
        sender_id = _find_text_value(
            raw,
            (
                "senderOpenDingTalkId",
                "senderId",
                "senderStaffId",
                "sender_id",
                "fromUserId",
            ),
        ) or "unknown"
        sender_name = _find_text_value(
            raw,
            ("sender", "senderName", "senderNick", "sender_name", "nick"),
        ) or sender_id
        messages.append(
            CollaborationMessage(
                message_id=message_id,
                sender_id=sender_id,
                sender_name=sender_name,
                content=content,
                created_at=created_at,
            )
        )
    return tuple(messages)


def _find_message_list(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, dict):
        for key in ("messages", "items", "list", "result", "data"):
            candidate = value.get(key)
            if isinstance(candidate, list) and all(
                isinstance(item, dict) for item in candidate
            ):
                return candidate
        for nested in value.values():
            found = _find_message_list(nested)
            if found:
                return found
    return []


def _message_content(value: Mapping[str, object]) -> str | None:
    for key in ("content", "text", "messageContent", "msgContent"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            normalized = candidate.strip()
            if normalized.startswith("{"):
                try:
                    decoded = json.loads(normalized)
                except json.JSONDecodeError:
                    return normalized
                nested = _find_text_value(decoded, ("content", "text", "title"))
                return nested or normalized
            return normalized
        if isinstance(candidate, dict):
            nested = _find_text_value(candidate, ("content", "text", "title"))
            if nested is not None:
                return nested
    return None


def _message_timestamp(value: Mapping[str, object]) -> int | None:
    for key in ("createTime", "createdAt", "created_at", "sendTime"):
        candidate = value.get(key)
        if isinstance(candidate, (int, float)):
            timestamp = int(candidate)
        elif isinstance(candidate, str):
            normalized = candidate.strip()
            if normalized.isdigit():
                timestamp = int(normalized)
            else:
                try:
                    timestamp = int(
                        datetime.strptime(
                            normalized,
                            "%Y-%m-%d %H:%M:%S",
                        ).timestamp()
                    )
                except ValueError:
                    continue
        else:
            continue
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return timestamp if timestamp > 0 else None
    return None


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required.")
    return value.strip()


def _resolve_dws_executable(value: str) -> str:
    executable = _require_text(value, "executable")
    path = Path(executable)
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return executable
    candidate = (
        path.parent.parent
        / "dingtalk-workspace-cli"
        / "vendor"
        / "dws.exe"
    )
    if candidate.is_file():
        return str(candidate.resolve())
    return executable


def _shorten(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def _domain_label(domain: str) -> str:
    return {
        "hr": "人事",
        "shareholder": "股东业务",
        "activity": "活动",
        "finance": "财务与薪酬",
        "operations": "门店运营",
        "unknown": "待判断",
    }.get(domain, domain)


def _reason_label(reason: str | None) -> str:
    return {
        "knowledge_gap": "知识库暂时没有可靠答案",
        "system_unavailable": "问答服务暂时不可用",
        "sensitive_personal_data": "涉及个人信息，需要人工处理",
        "high_impact_finance": "涉及重要财务事项，需要人工确认",
    }.get(reason, reason or "需要人工进一步确认")
