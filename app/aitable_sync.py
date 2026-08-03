from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Mapping, Protocol, Sequence

from app.dingtalk_adapter import DingTalkInteraction
from app.dingtalk_collaboration_closure import CollaborationDraft


@dataclass(frozen=True)
class AITableFieldMap:
    ticket_code: str
    asked_at: str
    domain: str
    question: str
    original_answer: str
    bot_action: str
    feedback: str
    status: str
    reason: str
    owner: str
    final_answer: str
    knowledge_status: str
    collaboration_status: str
    collaboration_group_name: str
    root_cause: str
    knowledge_candidate: str


@dataclass(frozen=True)
class AITableSyncResult:
    total: int
    created: int
    updated: int


class AITableClient(Protocol):
    def list_ticket_records(self, ticket_field_id: str) -> dict[str, str]: ...

    def create_records(self, records: list[dict[str, object]]) -> None: ...

    def update_records(
        self,
        records: list[tuple[str, dict[str, object]]],
    ) -> None: ...


class DwsAITableError(RuntimeError):
    pass


DwsJsonRunner = Callable[[tuple[str, ...]], dict[str, object]]


class DwsAITableClient:
    """Small DWS adapter used by the out-of-band board sync worker."""

    def __init__(
        self,
        *,
        base_id: str,
        table_id: str,
        runner: DwsJsonRunner,
    ) -> None:
        self._base_id = _required(base_id, "base_id")
        self._table_id = _required(table_id, "table_id")
        self._runner = runner

    def list_ticket_records(self, ticket_field_id: str) -> dict[str, str]:
        field_id = _required(ticket_field_id, "ticket_field_id")
        payload = self._runner(
            (
                "aitable",
                "record",
                "query",
                "--base-id",
                self._base_id,
                "--table-id",
                self._table_id,
                "--field-ids",
                field_id,
                "--all",
                "--format",
                "json",
            )
        )
        records = _records_from_payload(payload)
        result: dict[str, str] = {}
        for record in records:
            record_id = record.get("recordId")
            cells = record.get("cells")
            if not isinstance(record_id, str) or not isinstance(cells, dict):
                continue
            ticket_code = cells.get(field_id)
            if not isinstance(ticket_code, str) or not ticket_code.strip():
                continue
            normalized_ticket = ticket_code.strip()
            if normalized_ticket in result:
                raise DwsAITableError("Duplicate ticket code found in AI Table.")
            result[normalized_ticket] = record_id
        return result

    def create_records(self, records: list[dict[str, object]]) -> None:
        payloads = [{"cells": cells} for cells in records]
        self._write_batches("create", payloads)

    def update_records(
        self,
        records: list[tuple[str, dict[str, object]]],
    ) -> None:
        payloads = [
            {"recordId": _required(record_id, "record_id"), "cells": cells}
            for record_id, cells in records
        ]
        self._write_batches("update", payloads)

    def _write_batches(
        self,
        action: str,
        records: list[dict[str, object]],
    ) -> None:
        for offset in range(0, len(records), 100):
            batch = records[offset : offset + 100]
            with tempfile.TemporaryDirectory(prefix="dingtalk-aitable-") as directory:
                records_file = Path(directory) / "records.json"
                records_file.write_text(
                    json.dumps(batch, ensure_ascii=False),
                    encoding="utf-8",
                )
                self._runner(
                    (
                        "aitable",
                        "record",
                        action,
                        "--base-id",
                        self._base_id,
                        "--table-id",
                        self._table_id,
                        "--records-file",
                        str(records_file),
                        "--format",
                        "json",
                    )
                )


def subprocess_dws_runner(
    command: Sequence[str],
) -> DwsJsonRunner:
    prefix = tuple(_required(item, "command") for item in command)

    def run(args: tuple[str, ...]) -> dict[str, object]:
        completed = subprocess.run(
            (*prefix, *args),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise DwsAITableError("DWS AI Table operation failed.")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DwsAITableError("DWS returned invalid JSON.") from exc
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise DwsAITableError("DWS AI Table operation was not successful.")
        if payload.get("success") is not True and not isinstance(
            payload.get("data"), dict
        ):
            raise DwsAITableError("DWS AI Table operation returned invalid data.")
        return payload

    return run


_DOMAIN_LABELS = {
    "hr": "人事",
    "shareholder": "股东业务",
    "activity": "活动",
    "finance": "财务与薪酬",
    "operations": "门店运营",
    "unknown": "待判断",
}
_FEEDBACK_LABELS = {
    None: "未反馈",
    "resolved": "已解决",
    "unresolved": "未解决",
    "human_requested": "请求人工",
}
_STATUS_LABELS = {
    "awaiting_feedback": "等待反馈",
    "awaiting_clarification": "等待补充",
    "needs_human": "待人工",
    "resolved": "已解决",
    "closed": "已关闭",
}
_KNOWLEDGE_LABELS = {
    "not_reviewed": "待审核",
    "approved": "可复用",
    "not_reusable": "不可复用",
}
_COLLABORATION_LABELS = {
    None: "",
    "active": "沟通中",
    "resolved": "已结案",
}
_REASON_LABELS = {
    "knowledge_gap": "现有知识不足",
    "pending_business_rule": "制度口径尚未确认",
    "missing_business_context": "缺少必要业务信息",
    "out_of_scope_topic": "超出公司内部服务范围",
    "out_of_scope_sensitive_topic": "超出范围且涉及敏感话题",
    "sensitive_personal_data": "涉及敏感个人信息",
    "sensitive_financial_data": "涉及敏感财务信息",
    "controlled_financial_data": "涉及受控财务数据",
    "system_unavailable": "问答服务暂时不可用",
}
_GREETINGS = frozenset(
    {
        "你好",
        "您好",
        "你好呀",
        "你好啊",
        "哈喽",
        "嗨",
        "在吗",
        "早",
        "早上好",
        "下午好",
        "晚上好",
        "hello",
        "hi",
    }
)


def interaction_to_cells(
    interaction: DingTalkInteraction,
    fields: AITableFieldMap,
    draft: CollaborationDraft | None = None,
) -> dict[str, object]:
    """Map one already-redacted interaction to the privacy-safe board schema."""

    cells: dict[str, object] = {
        fields.ticket_code: interaction.ticket_code,
        fields.asked_at: datetime.fromtimestamp(interaction.created_at).strftime(
            "%Y-%m-%d %H:%M"
        ),
        fields.domain: _DOMAIN_LABELS[interaction.domain],
        fields.question: interaction.question,
        fields.original_answer: interaction.answer,
        fields.bot_action: _action_label(interaction),
        fields.feedback: _FEEDBACK_LABELS[interaction.feedback],
        fields.status: _STATUS_LABELS[interaction.status],
        fields.reason: _REASON_LABELS.get(
            interaction.reason,
            interaction.reason or "",
        ),
        fields.knowledge_status: _KNOWLEDGE_LABELS[
            interaction.knowledge_status
        ],
        fields.collaboration_status: _COLLABORATION_LABELS[
            interaction.collaboration_status
        ],
    }
    if interaction.owner:
        cells[fields.owner] = interaction.owner
    if interaction.final_answer:
        cells[fields.final_answer] = interaction.final_answer
    if interaction.collaboration_group_name:
        cells[fields.collaboration_group_name] = (
            interaction.collaboration_group_name
        )
    if draft is not None and draft.ticket_code == interaction.ticket_code:
        cells[fields.root_cause] = draft.root_cause
        cells[fields.knowledge_candidate] = draft.knowledge_candidate
    return cells


def sync_interactions(
    *,
    interactions: Sequence[DingTalkInteraction],
    client: AITableClient,
    fields: AITableFieldMap,
    drafts_by_ticket: Mapping[str, CollaborationDraft] | None = None,
) -> AITableSyncResult:
    drafts = drafts_by_ticket or {}
    existing = client.list_ticket_records(fields.ticket_code)
    creates: list[dict[str, object]] = []
    updates: list[tuple[str, dict[str, object]]] = []
    for interaction in interactions:
        cells = interaction_to_cells(
            interaction,
            fields,
            drafts.get(interaction.ticket_code),
        )
        record_id = existing.get(interaction.ticket_code)
        if record_id is None:
            creates.append(cells)
        else:
            updates.append((record_id, cells))
    if creates:
        client.create_records(creates)
    if updates:
        client.update_records(updates)
    return AITableSyncResult(
        total=len(interactions),
        created=len(creates),
        updated=len(updates),
    )


def _action_label(interaction: DingTalkInteraction) -> str:
    if interaction.collaboration_status == "active":
        return "协作群处理中"
    if interaction.collaboration_status == "resolved":
        return "协作群已结案"
    if interaction.action == "handoff":
        return "转负责人"
    if interaction.action == "clarify":
        return "要求补充"
    if interaction.action == "deny":
        if interaction.reason in (
            "out_of_scope_topic",
            "out_of_scope_sensitive_topic",
        ):
            return "范围外关闭"
        return "安全拒绝"
    normalized = "".join(interaction.question.casefold().split()).strip(
        "，。；、,.!?！？~～"
    )
    if interaction.domain == "unknown" and normalized in _GREETINGS:
        return "寒暄关闭"
    return "已回答"


def _records_from_payload(
    payload: dict[str, object],
) -> list[dict[str, object]]:
    if payload.get("success") is False:
        raise DwsAITableError("DWS AI Table query was not successful.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DwsAITableError("DWS AI Table query returned invalid data.")
    records = data.get("records")
    if not isinstance(records, list):
        raise DwsAITableError("DWS AI Table query returned invalid records.")
    return [record for record in records if isinstance(record, dict)]


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required.")
    return value.strip()
