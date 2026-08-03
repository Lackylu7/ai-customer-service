from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence

from dingtalk_stream import AckMessage, CallbackHandler

from app.dingtalk_adapter import DingTalkInteraction, DingTalkState
from app.dingtalk_collaboration_closure import DingTalkCollaborationClosureService
from app.dingtalk_messaging import DingTalkProactiveMessenger


LOGGER = logging.getLogger("dingtalk-collaboration-cards")
_CARD_PREFIX = "internal-service-closure-v2-"
_TICKET_PATTERN = re.compile(r"CS-\d{8}-\d{6}")
_GENERATE_ACTION = "generate_closure"
_CONFIRM_ACTION = "confirm_closure"
_COMPLETE_ACTION = "complete_and_publish"
_CONTINUE_ACTION = "continue_dialogue"
_KNOWN_ACTIONS = frozenset(
    {_GENERATE_ACTION, _CONFIRM_ACTION, _COMPLETE_ACTION, _CONTINUE_ACTION}
)


class DingTalkCollaborationCardService:
    def __init__(
        self,
        *,
        state: DingTalkState,
        closure: DingTalkCollaborationClosureService,
        messenger: DingTalkProactiveMessenger,
        operator_user_ids: Sequence[str] = (),
    ) -> None:
        self._state = state
        self._closure = closure
        self._messenger = messenger
        self._operator_user_ids = frozenset(operator_user_ids)

    def send_open_card(
        self,
        *,
        interaction: DingTalkInteraction,
        group_id: str,
        status: str = "负责人沟通中",
    ) -> str:
        card_biz_id = _card_biz_id(interaction.ticket_code)
        self._messenger.send_group_card(
            group_id=group_id,
            card_biz_id=card_biz_id,
            card_data=_open_card(interaction, status=status),
        )
        return card_biz_id

    def handle_action(
        self,
        *,
        card_biz_id: str,
        sender_id: str,
        action: str,
    ) -> str:
        result, _ = self.handle_action_for_callback(
            card_biz_id=card_biz_id,
            sender_id=sender_id,
            action=action,
        )
        return result

    def handle_action_for_callback(
        self,
        *,
        card_biz_id: str,
        sender_id: str,
        action: str,
    ) -> tuple[str, dict[str, object] | None]:
        ticket_code = _ticket_from_card_biz_id(card_biz_id)
        interaction = self._state.get_interaction(ticket_code)
        if interaction is None or not interaction.collaboration_group_id:
            return "没有找到这张卡片对应的协作工单。", None
        if not self._closure.can_manage(
            interaction=interaction,
            sender_id=sender_id,
        ):
            result = "只有该业务板块负责人或项目管理员可以操作结案卡片。"
            return result, _open_card(interaction, status=result)

        if action == _CONTINUE_ACTION:
            card_data = _open_card(
                interaction,
                status="继续沟通中；问题解决后可随时点击“完成并沉淀”。",
            )
            self._messenger.update_group_card(
                card_biz_id=card_biz_id,
                card_data=card_data,
            )
            return "已保持为继续沟通状态。", card_data

        actor_name = (
            "项目管理员"
            if sender_id in self._operator_user_ids
            else "业务负责人"
        )
        command = {
            _GENERATE_ACTION: "总结并结案",
            _CONFIRM_ACTION: "确认结案",
            _COMPLETE_ACTION: "完成并沉淀",
        }[action]
        result = self._closure.handle_group_command(
            sender_id=sender_id,
            sender_name=actor_name,
            group_id=interaction.collaboration_group_id,
            text=command,
        )
        if result is None:
            result = "卡片操作未能生成有效结果，请使用文字指令重试。"

        latest = self._state.get_interaction(ticket_code) or interaction
        if action == _GENERATE_ACTION:
            card_data = (
                _draft_card(latest, result)
                if "结案草稿" in result
                else _open_card(latest, status=result)
            )
            self._messenger.update_group_card(
                card_biz_id=card_biz_id,
                card_data=card_data,
            )
            return result, card_data

        card_data = (
            _resolved_card(latest, result)
            if "已结案" in result
            else _draft_card(latest, result)
        )
        self._messenger.update_group_card(
            card_biz_id=card_biz_id,
            card_data=card_data,
        )
        return result, card_data


class DingTalkCollaborationCardHandler(CallbackHandler):
    def __init__(self, *, service: DingTalkCollaborationCardService) -> None:
        super().__init__()
        self._service = service

    async def process(self, callback) -> tuple[int, object]:
        data = callback.data if isinstance(callback.data, Mapping) else {}
        card_biz_id = _text_value(data.get("outTrackId"))
        sender_id = _text_value(data.get("userId"))
        action = _find_action(_json_object(data.get("content")))
        if not card_biz_id or not sender_id or action is None:
            LOGGER.warning("ignored invalid collaboration card callback")
            return AckMessage.STATUS_OK, _callback_ack("ignored")
        try:
            result, card_data = await asyncio.to_thread(
                self._service.handle_action_for_callback,
                card_biz_id=card_biz_id,
                sender_id=sender_id,
                action=action,
            )
        except Exception:
            LOGGER.exception(
                "failed collaboration card action card=%s action=%s",
                card_biz_id,
                action,
            )
            return AckMessage.STATUS_OK, _callback_ack("failed")
        LOGGER.info(
            "processed collaboration card action card=%s action=%s result=%s",
            card_biz_id,
            action,
            result[:120],
        )
        return AckMessage.STATUS_OK, _callback_ack(action, card_data)


def _card_biz_id(ticket_code: str) -> str:
    match = _TICKET_PATTERN.fullmatch(ticket_code.strip())
    if match is None:
        raise ValueError("ticket_code is invalid.")
    return f"{_CARD_PREFIX}{match.group(0)}"


def _ticket_from_card_biz_id(card_biz_id: str) -> str:
    normalized = card_biz_id.strip()
    if not normalized.startswith(_CARD_PREFIX):
        raise ValueError("card_biz_id is not a collaboration card.")
    ticket_code = normalized[len(_CARD_PREFIX) :]
    if _TICKET_PATTERN.fullmatch(ticket_code) is None:
        raise ValueError("card_biz_id contains an invalid ticket code.")
    return ticket_code


def _open_card(
    interaction: DingTalkInteraction,
    *,
    status: str = "负责人沟通中",
) -> dict[str, object]:
    markdown = (
        f"### {_domain_label(interaction.domain)}协作处理\n\n"
        f"**工单：** {interaction.ticket_code}\n\n"
        f"**员工问题：** {_shorten(interaction.question, 500)}\n\n"
        f"**当前状态：** {status}\n\n"
        "负责人已给出明确答复后，由业务负责人或项目管理员一键完成。"
    )
    return _card(
        title="内部服务协作",
        markdown=markdown,
        actions=(
            _button(_COMPLETE_ACTION, "完成并沉淀", primary=True),
            _button(_CONTINUE_ACTION, "继续沟通"),
        ),
    )


def _draft_card(
    interaction: DingTalkInteraction,
    draft_preview: str,
) -> dict[str, object]:
    markdown = (
        f"### 结案草稿 · {interaction.ticket_code}\n\n"
        f"{_shorten(draft_preview, 6000)}\n\n"
        "确认内容正确后再结案；需要改答案时可继续使用“修改结案：正确答案”。"
    )
    return _card(
        title="结案草稿待确认",
        markdown=markdown,
        actions=(
            _button(_CONFIRM_ACTION, "确认并结案", primary=True),
            _button(_CONTINUE_ACTION, "继续沟通"),
        ),
    )


def _resolved_card(
    interaction: DingTalkInteraction,
    result: str,
) -> dict[str, object]:
    markdown = (
        f"### 已结案 · {interaction.ticket_code}\n\n"
        f"{_shorten(result, 1500)}\n\n"
        "负责人确认的结论已通过自动检查并写入正式知识库。"
    )
    return _card(title="协作问题已结案", markdown=markdown, actions=())


def _card(
    *,
    title: str,
    markdown: str,
    actions: Sequence[dict[str, object]],
) -> dict[str, object]:
    full_json: dict[str, object] = {
        "order": ["msgTitle", "staticMsgContent"],
    }
    if actions:
        full_json["order"].append("msgButtons")
        full_json["msgButtons"] = list(actions)
    return {
        "msgTitle": title,
        "staticMsgContent": markdown,
        "sys_full_json_obj": json.dumps(full_json, ensure_ascii=False),
    }


def _button(action: str, label: str, *, primary: bool = False) -> dict[str, object]:
    return {
        "text": label,
        "color": "blue" if primary else "gray",
        "id": action,
        "request": True,
    }


def _find_action(value: object) -> str | None:
    if isinstance(value, str):
        return value if value in _KNOWN_ACTIONS else None
    if isinstance(value, Mapping):
        for child in value.values():
            found = _find_action(child)
            if found is not None:
                return found
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            found = _find_action(child)
            if found is not None:
                return found
    return None


def _json_object(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _text_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _callback_ack(
    action: str,
    card_data: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if card_data is None:
        return {}
    return {
        "cardData": {
            "cardParamMap": {
                **card_data,
                "lastAction": action,
                "flowStatus": "3",
            }
        }
    }


def _shorten(value: str, limit: int) -> str:
    normalized = value.strip()
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _domain_label(domain: str) -> str:
    return {
        "shareholder": "股东业务",
        "finance": "财务",
        "operations": "运营",
        "hr": "人事",
        "activity": "活动",
    }.get(domain, "内部服务")
