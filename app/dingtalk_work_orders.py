from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from app.dingtalk_adapter import DingTalkInteraction, DingTalkState


_TICKET_PATTERN = r"CS-\d{8}-\d{6}"
_CLAIM_PATTERN = re.compile(rf"^\s*领取\s+({_TICKET_PATTERN})\s*$", re.IGNORECASE)
_VIEW_PATTERN = re.compile(rf"^\s*查看\s+({_TICKET_PATTERN})\s*$", re.IGNORECASE)
_ANSWER_PATTERN = re.compile(
    rf"^\s*(?:答复|回复)\s+({_TICKET_PATTERN})(?:\s+([\s\S]*?))?\s*$",
    re.IGNORECASE,
)
_CONTEXT_CLAIM_PATTERN = re.compile(
    r"^\s*(?:领取|接单|我来(?:领取|处理)|"
    r"这个(?:问题|工单)?我来处理|我来处理这个(?:问题|工单)?)\s*[。！!]?\s*$"
)
_CONTEXT_VIEW_PATTERN = re.compile(
    r"^\s*(?:查看|查看详情|看看详情|查看(?:这个|当前)(?:问题|工单))\s*[。！!]?\s*$"
)
_CONTEXT_ANSWER_PATTERN = re.compile(
    r"^\s*(?:(?:我要|我想)(?:答复|回复)(?:这个|该|当前)?(?:问题|工单)?"
    r"(?:是|为)?\s*[:：]?\s*|"
    r"(?:答复|回复)(?:这个|该|当前)?(?:问题|工单)?(?:是|为)?\s*[:：]\s*)"
    r"([\s\S]*?)\s*$"
)
_STANDARD_ANSWER_PATTERN = re.compile(
    rf"^\s*(?:设为|标记为|沉淀为)?标准答案(?:\s+({_TICKET_PATTERN}))?\s*[。！!]?\s*$",
    re.IGNORECASE,
)
_CASE_ONLY_PATTERN = re.compile(
    rf"^\s*(?:仅本次(?:答复|使用)|只用于本次)(?:\s+({_TICKET_PATTERN}))?\s*[。！!]?\s*$",
    re.IGNORECASE,
)
_ESCALATION_REASONS = frozenset(
    {"sensitive_personal_data", "high_impact_finance"}
)


class ProactiveMessenger(Protocol):
    def send_text(self, user_ids: tuple[str, ...], text: str) -> None: ...


@dataclass(frozen=True)
class WorkOrderCommandResult:
    text: str


class DingTalkWorkOrderService:
    def __init__(
        self,
        *,
        state: DingTalkState,
        messenger: ProactiveMessenger,
        handler_user_ids: frozenset[str],
        handler_user_ids_by_domain: Mapping[str, str] | None = None,
    ) -> None:
        if not handler_user_ids:
            raise ValueError("At least one work-order handler is required.")
        self._state = state
        self._messenger = messenger
        self._handler_user_ids = handler_user_ids
        self._handler_user_ids_by_domain = dict(handler_user_ids_by_domain or {})
        unknown_handlers = set(self._handler_user_ids_by_domain.values()).difference(
            handler_user_ids
        )
        if unknown_handlers:
            raise ValueError("Department owners must also be work-order handlers.")
        self._active_ticket_by_handler: dict[str, str] = {}
        self._last_answered_ticket_by_handler: dict[str, str] = {}
        self._active_ticket_lock = Lock()

    def notify_handlers(self, interaction: DingTalkInteraction) -> None:
        department_owner = self._handler_user_ids_by_domain.get(interaction.domain)
        recipients = (
            (department_owner,)
            if department_owner is not None
            else tuple(sorted(self._handler_user_ids))
        )
        self._messenger.send_text(
            recipients,
            (
                f"【待人工工单】{interaction.ticket_code}\n"
                f"业务板块：{_domain_label(interaction.domain)}\n"
                f"员工问题：{_shorten(interaction.question, 1000)}\n"
                f"转人工原因：{_reason_label(interaction.reason)}\n\n"
                "可以直接回复：\n"
                "- 我来处理\n"
                "- 查看详情\n"
                "- 答复：核实后的答案\n\n"
                "如同时有多张工单，机器人会提示选择工单编号。"
            ),
        )
        for handler_id in recipients:
            self._remember_active(handler_id, interaction.ticket_code)

    def handle_command(
        self,
        *,
        sender_id: str,
        sender_name: str,
        text: str,
        is_direct: bool,
    ) -> WorkOrderCommandResult | None:
        if sender_id not in self._handler_user_ids:
            return None

        claim_match = _CLAIM_PATTERN.fullmatch(text)
        view_match = _VIEW_PATTERN.fullmatch(text)
        answer_match = _ANSWER_PATTERN.fullmatch(text)
        context_claim_match = _CONTEXT_CLAIM_PATTERN.fullmatch(text)
        context_view_match = _CONTEXT_VIEW_PATTERN.fullmatch(text)
        context_answer_match = _CONTEXT_ANSWER_PATTERN.fullmatch(text)
        standard_answer_match = _STANDARD_ANSWER_PATTERN.fullmatch(text)
        case_only_match = _CASE_ONLY_PATTERN.fullmatch(text)
        if (
            claim_match is None
            and view_match is None
            and answer_match is None
            and context_claim_match is None
            and context_view_match is None
            and context_answer_match is None
            and standard_answer_match is None
            and case_only_match is None
        ):
            return None
        if not is_direct:
            return WorkOrderCommandResult("请私聊机器人处理人工工单，避免在群内暴露业务信息。")

        if claim_match is not None:
            return self._claim(
                ticket_code=claim_match.group(1).upper(),
                sender_id=sender_id,
                sender_name=sender_name,
            )
        if view_match is not None:
            ticket_code = view_match.group(1).upper()
            self._remember_active(sender_id, ticket_code)
            return self._view(ticket_code)
        if answer_match is not None:
            return self._answer(
                ticket_code=answer_match.group(1).upper(),
                sender_id=sender_id,
                final_answer=answer_match.group(2) or "",
            )
        if standard_answer_match is not None or case_only_match is not None:
            matched = standard_answer_match or case_only_match
            assert matched is not None
            interaction, review_error = self._resolve_review_context(
                sender_id,
                matched.group(1),
            )
            if interaction is None:
                return WorkOrderCommandResult(review_error)
            return self._review_answer(
                interaction=interaction,
                sender_id=sender_id,
                sender_name=sender_name,
                reusable=standard_answer_match is not None,
            )

        interaction, context_error = self._resolve_context(sender_id)
        if interaction is None:
            return WorkOrderCommandResult(context_error)
        if context_claim_match is not None:
            return self._claim(
                ticket_code=interaction.ticket_code,
                sender_id=sender_id,
                sender_name=sender_name,
            )
        if context_view_match is not None:
            self._remember_active(sender_id, interaction.ticket_code)
            return self._view(interaction.ticket_code)

        assert context_answer_match is not None
        if interaction.owner_id != sender_id:
            claim_result = self._claim(
                ticket_code=interaction.ticket_code,
                sender_id=sender_id,
                sender_name=sender_name,
            )
            claimed = self._state.get_interaction(interaction.ticket_code)
            if claimed is None or claimed.owner_id != sender_id:
                return claim_result
        return self._answer(
            ticket_code=interaction.ticket_code,
            sender_id=sender_id,
            final_answer=context_answer_match.group(1) or "",
        )

    def _claim(
        self,
        *,
        ticket_code: str,
        sender_id: str,
        sender_name: str,
    ) -> WorkOrderCommandResult:
        interaction = self._state.get_interaction(ticket_code)
        if interaction is None:
            return WorkOrderCommandResult(f"没有找到工单 {ticket_code}。")
        claimed = self._state.claim_interaction(
            ticket_code=ticket_code,
            owner=sender_name or "钉钉处理人",
            owner_id=sender_id,
        )
        if claimed is None:
            owner = interaction.owner or "其他负责人"
            return WorkOrderCommandResult(
                f"工单 {ticket_code} 当前无法领取，状态为"
                f"“{_status_label(interaction.status)}”，负责人为“{owner}”。"
            )
        self._remember_active(sender_id, ticket_code)
        return WorkOrderCommandResult(
            f"已领取工单 {ticket_code}。\n"
            f"员工问题：{claimed.question}\n\n"
            "核实后直接回复：答复：核实后的答案"
        )

    def _view(self, ticket_code: str) -> WorkOrderCommandResult:
        interaction = self._state.get_interaction(ticket_code)
        if interaction is None:
            return WorkOrderCommandResult(f"没有找到工单 {ticket_code}。")
        return WorkOrderCommandResult(
            f"工单：{ticket_code}\n"
            f"状态：{_status_label(interaction.status)}\n"
            f"业务板块：{_domain_label(interaction.domain)}\n"
            f"负责人：{interaction.owner or '未领取'}\n"
            f"员工问题：{_shorten(interaction.question, 1000)}\n"
            f"机器人原答：{_shorten(interaction.answer, 1500)}"
        )

    def _answer(
        self,
        *,
        ticket_code: str,
        sender_id: str,
        final_answer: str,
    ) -> WorkOrderCommandResult:
        interaction = self._state.get_interaction(ticket_code)
        if interaction is None:
            return WorkOrderCommandResult(f"没有找到工单 {ticket_code}。")
        if interaction.status != "needs_human":
            return WorkOrderCommandResult(
                f"工单 {ticket_code} 已经是“{_status_label(interaction.status)}”，"
                "不能重复答复。"
            )
        if interaction.owner_id != sender_id:
            return WorkOrderCommandResult(
                f"请先由本人领取工单 {ticket_code}，再提交人工答复。"
            )

        normalized_answer = final_answer.strip()
        if not normalized_answer:
            return WorkOrderCommandResult("人工答复不能为空，请填写核实后的正确答案。")
        if len(normalized_answer) > 3500:
            return WorkOrderCommandResult(
                "人工答复不能超过3500个字符，请精简后重新提交。"
            )
        self._messenger.send_text(
            (interaction.sender_id,),
            (
                f"您提交的问题 {ticket_code} 已由人工负责人处理。\n\n"
                f"{normalized_answer}\n\n"
                "如仍未解决，请直接回复“未解决”。"
            ),
        )
        answered = self._state.answer_interaction(
            ticket_code=ticket_code,
            owner_id=sender_id,
            final_answer=normalized_answer,
        )
        if answered is None:
            return WorkOrderCommandResult(
                f"工单 {ticket_code} 状态刚刚发生变化，请重新查看后再处理。"
            )
        self._forget_active(sender_id, ticket_code)
        self._remember_answered(sender_id, ticket_code)
        return WorkOrderCommandResult(
            f"已将人工答案回复给员工，工单 {ticket_code} 已完成。\n"
            "如果这份答案可供同类问题复用，请回复“设为标准答案”；"
            "如果只适用于本次情况，请回复“仅本次答复”。"
        )

    def _review_answer(
        self,
        *,
        interaction: DingTalkInteraction,
        sender_id: str,
        sender_name: str,
        reusable: bool,
    ) -> WorkOrderCommandResult:
        if reusable and interaction.reason in _ESCALATION_REASONS:
            return WorkOrderCommandResult(
                f"工单 {interaction.ticket_code} 涉及敏感个人信息或高影响财务事项，"
                "需要升级确认后才能设为标准答案。当前答案仍只用于本次人工处理。"
            )
        status = "approved" if reusable else "not_reusable"
        updated = self._state.review_answer(
            ticket_code=interaction.ticket_code,
            owner_id=sender_id,
            reviewed_by=sender_name or interaction.owner or "部门负责人",
            knowledge_status=status,
        )
        if updated is None:
            return WorkOrderCommandResult(
                f"工单 {interaction.ticket_code} 的知识处理状态已发生变化，请查看后再操作。"
            )
        self._forget_answered(sender_id, interaction.ticket_code)
        if reusable:
            return WorkOrderCommandResult(
                f"已将工单 {interaction.ticket_code} 标记为标准答案，"
                "现已进入可发布候选状态；知识库发布前仍会执行自动检查。"
            )
        return WorkOrderCommandResult(
            f"已将工单 {interaction.ticket_code} 标记为仅本次答复，"
            "不会沉淀到公共知识库。"
        )

    def _resolve_context(
        self,
        sender_id: str,
    ) -> tuple[DingTalkInteraction | None, str]:
        with self._active_ticket_lock:
            active_ticket = self._active_ticket_by_handler.get(sender_id)
        if active_ticket is not None:
            active = self._state.get_interaction(active_ticket)
            if (
                active is not None
                and active.status == "needs_human"
                and active.owner_id in (None, sender_id)
            ):
                return active, ""
            self._forget_active(sender_id, active_ticket)

        pending = self._state.list_interactions(status="needs_human")
        owned = tuple(item for item in pending if item.owner_id == sender_id)
        if len(owned) == 1:
            return owned[0], ""
        if len(owned) > 1:
            return None, _ambiguous_ticket_text(owned)

        unclaimed = tuple(
            item for item in pending if item.owner is None and item.owner_id is None
        )
        if len(unclaimed) == 1:
            return unclaimed[0], ""
        if len(unclaimed) > 1:
            return None, _ambiguous_ticket_text(unclaimed)
        return None, "当前没有可处理的待人工工单。"

    def _resolve_review_context(
        self,
        sender_id: str,
        ticket_code: str | None,
    ) -> tuple[DingTalkInteraction | None, str]:
        selected_ticket = ticket_code
        if selected_ticket is None:
            with self._active_ticket_lock:
                selected_ticket = self._last_answered_ticket_by_handler.get(sender_id)
        if selected_ticket is not None:
            interaction = self._state.get_interaction(selected_ticket.upper())
            if (
                interaction is not None
                and interaction.status == "resolved"
                and interaction.owner_id == sender_id
                and interaction.knowledge_status == "not_reviewed"
            ):
                return interaction, ""
            self._forget_answered(sender_id, selected_ticket)

        candidates = tuple(
            interaction
            for interaction in self._state.list_interactions()
            if interaction.status == "resolved"
            and interaction.owner_id == sender_id
            and interaction.knowledge_status == "not_reviewed"
        )
        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1:
            return None, _ambiguous_review_text(candidates)
        return None, "当前没有等待你判断是否沉淀的人工答案。"

    def _remember_active(self, sender_id: str, ticket_code: str) -> None:
        with self._active_ticket_lock:
            self._active_ticket_by_handler[sender_id] = ticket_code

    def _forget_active(self, sender_id: str, ticket_code: str) -> None:
        with self._active_ticket_lock:
            if self._active_ticket_by_handler.get(sender_id) == ticket_code:
                self._active_ticket_by_handler.pop(sender_id, None)

    def _remember_answered(self, sender_id: str, ticket_code: str) -> None:
        with self._active_ticket_lock:
            self._last_answered_ticket_by_handler[sender_id] = ticket_code

    def _forget_answered(self, sender_id: str, ticket_code: str) -> None:
        with self._active_ticket_lock:
            if self._last_answered_ticket_by_handler.get(sender_id) == ticket_code:
                self._last_answered_ticket_by_handler.pop(sender_id, None)


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
        "knowledge_gap": "现有知识不足",
        "system_unavailable": "问答服务暂时不可用",
        "sensitive_personal_data": "涉及敏感个人信息",
        "high_impact_finance": "涉及高影响财务事项",
    }.get(reason, reason or "需要人工核实")


def _status_label(status: str) -> str:
    return {
        "awaiting_feedback": "等待员工反馈",
        "awaiting_clarification": "等待补充信息",
        "needs_human": "待人工处理",
        "resolved": "已解决",
        "closed": "已关闭",
    }.get(status, status)


def _shorten(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def _ambiguous_ticket_text(
    interactions: tuple[DingTalkInteraction, ...],
) -> str:
    recent = interactions[-5:]
    details = "\n".join(
        f"- {item.ticket_code}：{_shorten(item.question, 60)}" for item in recent
    )
    return (
        "当前有多张待处理工单，无法安全判断你指的是哪一张：\n"
        f"{details}\n\n"
        "请发送“领取 工单编号”“查看 工单编号”或"
        "“答复 工单编号 核实后的答案”。"
    )


def _ambiguous_review_text(
    interactions: tuple[DingTalkInteraction, ...],
) -> str:
    recent = interactions[-5:]
    details = "\n".join(
        f"- {item.ticket_code}：{_shorten(item.question, 60)}" for item in recent
    )
    return (
        "当前有多份人工答案等待判断是否沉淀：\n"
        f"{details}\n\n"
        "请发送“设为标准答案 工单编号”或“仅本次答复 工单编号”。"
    )
