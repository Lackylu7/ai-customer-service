from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Protocol, Sequence

from app.assistant_brain import AssistantBrain
from app.dify_client import CreatedDocument, DifyClient
from app.dingtalk_adapter import DingTalkInteraction, DingTalkState
from app.dingtalk_collaboration import (
    CollaborationGroupError,
    CollaborationMessage,
    CollaborationTranscriptGateway,
)
from app.operations_privacy import redact_question


_TICKET_PATTERN = r"CS-\d{8}-\d{6}"
_SUMMARIZE_PATTERN = re.compile(
    rf"^\s*总结并结案(?:\s+({_TICKET_PATTERN}))?\s*[。！!]?\s*$",
    re.IGNORECASE,
)
_CONFIRM_PATTERN = re.compile(r"^\s*确认结案\s*[。！!]?\s*$")
_COMPLETE_PATTERN = re.compile(r"^\s*完成并沉淀\s*[。！!]?\s*$")
_REVISE_PATTERN = re.compile(
    r"^\s*修改结案\s*[:：]\s*([\s\S]+?)\s*$"
)
_NON_BUSINESS_MARKERS = (
    "技术测试",
    "非业务答复",
    "请勿作为结案依据",
)
_NON_ANSWER_TEXTS = frozenset(
    {"喂", "测试", "收到", "好的", "好", "ok", "okay", "可以可以"}
)


@dataclass(frozen=True)
class CollaborationDraft:
    ticket_code: str
    group_id: str
    final_answer: str
    root_cause: str
    knowledge_candidate: str
    created_at: int


class CollaborationKnowledgePublisher(Protocol):
    def publish(
        self,
        *,
        interaction: DingTalkInteraction,
        draft: CollaborationDraft,
    ) -> CreatedDocument: ...


class DifyCollaborationKnowledgePublisher:
    """Publish an explicitly approved collaboration conclusion to Dify."""

    def __init__(
        self,
        *,
        dataset_id: str,
        client: DifyClient,
        indexing_technique: str = "high_quality",
    ) -> None:
        self._dataset_id = _require_text(dataset_id, "dataset_id")
        if indexing_technique not in {"economy", "high_quality"}:
            raise ValueError("Unsupported Dify indexing technique.")
        self._client = client
        self._indexing_technique = indexing_technique

    def publish(
        self,
        *,
        interaction: DingTalkInteraction,
        draft: CollaborationDraft,
    ) -> CreatedDocument:
        question = redact_question(interaction.question)
        final_answer = redact_question(draft.final_answer)
        root_cause = redact_question(draft.root_cause)
        candidate = redact_question(draft.knowledge_candidate)
        text = (
            f"# 内部服务知识 · {interaction.ticket_code}\n\n"
            f"业务板块：{interaction.domain}\n\n"
            f"## 员工常见问题\n\n{question}\n\n"
            f"## 经负责人确认的答复\n\n{final_answer}\n\n"
            f"## 可复用知识\n\n{candidate}\n\n"
            f"## 问题成因\n\n{root_cause}\n"
        )
        return self._client.create_document_by_text(
            dataset_id=self._dataset_id,
            name=f"协作沉淀-{interaction.ticket_code}",
            text=text,
            indexing_technique=self._indexing_technique,
        )


class CollaborationTranscriptStore(Protocol):
    def save_messages(
        self,
        *,
        ticket_code: str,
        group_id: str,
        messages: Sequence[CollaborationMessage],
    ) -> int: ...

    def list_messages(
        self,
        *,
        ticket_code: str,
    ) -> tuple[CollaborationMessage, ...]: ...

    def save_draft(self, draft: CollaborationDraft) -> None: ...

    def get_draft(self, *, group_id: str) -> CollaborationDraft | None: ...


class SQLiteCollaborationTranscriptStore:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def save_messages(
        self,
        *,
        ticket_code: str,
        group_id: str,
        messages: Sequence[CollaborationMessage],
    ) -> int:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        normalized_group = _require_text(group_id, "group_id")
        inserted = 0
        with self._connect() as connection:
            for message in messages:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO dingtalk_collaboration_messages (
                        message_id, ticket_code, group_id, sender_id, sender_name,
                        content, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.message_id,
                        normalized_ticket,
                        normalized_group,
                        message.sender_id,
                        message.sender_name,
                        redact_question(message.content),
                        message.created_at,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def list_messages(
        self,
        *,
        ticket_code: str,
    ) -> tuple[CollaborationMessage, ...]:
        normalized_ticket = _require_text(ticket_code, "ticket_code")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT message_id, sender_id, sender_name, content, created_at
                FROM dingtalk_collaboration_messages
                WHERE ticket_code = ?
                ORDER BY created_at, message_id
                """,
                (normalized_ticket,),
            ).fetchall()
        return tuple(
            CollaborationMessage(
                message_id=str(row["message_id"]),
                sender_id=str(row["sender_id"]),
                sender_name=str(row["sender_name"]),
                content=str(row["content"]),
                created_at=int(row["created_at"]),
            )
            for row in rows
        )

    def save_draft(self, draft: CollaborationDraft) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dingtalk_collaboration_drafts (
                    group_id, ticket_code, final_answer, root_cause,
                    knowledge_candidate, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    ticket_code = excluded.ticket_code,
                    final_answer = excluded.final_answer,
                    root_cause = excluded.root_cause,
                    knowledge_candidate = excluded.knowledge_candidate,
                    created_at = excluded.created_at
                """,
                (
                    draft.group_id,
                    draft.ticket_code,
                    draft.final_answer,
                    draft.root_cause,
                    draft.knowledge_candidate,
                    draft.created_at,
                ),
            )

    def get_draft(self, *, group_id: str) -> CollaborationDraft | None:
        normalized_group = _require_text(group_id, "group_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM dingtalk_collaboration_drafts WHERE group_id = ?
                """,
                (normalized_group,),
            ).fetchone()
        if row is None:
            return None
        return CollaborationDraft(
            ticket_code=str(row["ticket_code"]),
            group_id=str(row["group_id"]),
            final_answer=str(row["final_answer"]),
            root_cause=str(row["root_cause"]),
            knowledge_candidate=str(row["knowledge_candidate"]),
            created_at=int(row["created_at"]),
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dingtalk_collaboration_messages (
                    message_id TEXT PRIMARY KEY,
                    ticket_code TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_collaboration_messages_ticket
                ON dingtalk_collaboration_messages(ticket_code, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dingtalk_collaboration_drafts (
                    group_id TEXT PRIMARY KEY,
                    ticket_code TEXT NOT NULL,
                    final_answer TEXT NOT NULL,
                    root_cause TEXT NOT NULL,
                    knowledge_candidate TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class CollaborationSummarizer(Protocol):
    def summarize(
        self,
        *,
        interaction: DingTalkInteraction,
        messages: Sequence[CollaborationMessage],
        authoritative_sender_ids: frozenset[str] = frozenset(),
    ) -> CollaborationDraft: ...


class BrainCollaborationSummarizer:
    def __init__(self, brain: AssistantBrain) -> None:
        self._brain = brain

    def summarize(
        self,
        *,
        interaction: DingTalkInteraction,
        messages: Sequence[CollaborationMessage],
        authoritative_sender_ids: frozenset[str] = frozenset(),
    ) -> CollaborationDraft:
        transcript = "\n".join(
            (
                "[负责人确认] "
                if message.sender_id in authoritative_sender_ids
                and _is_authoritative_answer_candidate(message.content)
                else "[沟通上下文] "
            )
            + f"{message.sender_name or message.sender_id}：{message.content}"
            for message in messages
            if message.content.strip()
        )
        response = self._brain.send_message(
            query=(
                "你正在整理一段企业内部问题协作记录。只能依据下面的原始问题和"
                "群聊记录总结，不得补充记录中不存在的制度、数字或承诺。输出严格 JSON，"
                "字段为 final_answer、root_cause、knowledge_candidate。final_answer 是给员工的"
                "最终答复；root_cause 是员工产生疑问的原因；knowledge_candidate 是可复用的"
                "知识条目草稿。只有标记为[负责人确认]的发言可以作为最终业务结论，"
                "其他发言只用于理解上下文。若负责人没有给出清晰结论，请在对应字段"
                "明确写“仍需负责人补充”。\n\n"
                f"原始问题：{interaction.question}\n"
                f"群聊记录：\n{transcript[:12000]}"
            ),
            user=f"dingtalk-collaboration:{interaction.ticket_code}",
        )
        payload = _parse_json_object(response.answer)
        return CollaborationDraft(
            ticket_code=interaction.ticket_code,
            group_id=interaction.collaboration_group_id or "",
            final_answer=_required_payload_text(payload, "final_answer"),
            root_cause=_required_payload_text(payload, "root_cause"),
            knowledge_candidate=_required_payload_text(
                payload, "knowledge_candidate"
            ),
            created_at=int(time.time()),
        )


class DingTalkCollaborationClosureService:
    def __init__(
        self,
        *,
        state: DingTalkState,
        gateway: CollaborationTranscriptGateway,
        transcript_store: CollaborationTranscriptStore,
        summarizer: CollaborationSummarizer,
        handler_user_ids_by_domain: Mapping[str, str],
        operator_user_ids: Sequence[str] = (),
        sender_aliases_by_user_id: Mapping[str, str] | None = None,
        knowledge_publisher: CollaborationKnowledgePublisher | None = None,
    ) -> None:
        self._state = state
        self._gateway = gateway
        self._transcript_store = transcript_store
        self._summarizer = summarizer
        self._handler_user_ids_by_domain = dict(handler_user_ids_by_domain)
        self._operator_user_ids = frozenset(
            _require_text(user_id, "operator_user_id")
            for user_id in operator_user_ids
        )
        self._sender_aliases_by_user_id = {
            _require_text(user_id, "alias_user_id"): _require_text(
                alias,
                "sender_alias",
            )
            for user_id, alias in (sender_aliases_by_user_id or {}).items()
        }
        self._knowledge_publisher = knowledge_publisher

    def is_active_group(self, *, group_id: str) -> bool:
        normalized_group_id = group_id.strip()
        if not normalized_group_id:
            return False
        return any(
            interaction.collaboration_group_id == normalized_group_id
            and interaction.collaboration_status == "active"
            for interaction in self._state.list_interactions(status="needs_human")
        )

    def can_manage(self, *, interaction: DingTalkInteraction, sender_id: str) -> bool:
        owner_id = self._handler_user_ids_by_domain.get(interaction.domain)
        return sender_id == owner_id or sender_id in self._operator_user_ids

    def record_group_message(
        self,
        *,
        message_id: str,
        sender_id: str,
        sender_name: str,
        group_id: str,
        content: str,
        created_at: int,
    ) -> bool:
        interaction, _ = self._resolve_context(
            group_id=group_id,
            ticket_code=None,
        )
        if interaction is None:
            return False
        self._transcript_store.save_messages(
            ticket_code=interaction.ticket_code,
            group_id=group_id,
            messages=(
                CollaborationMessage(
                    message_id=_require_text(message_id, "message_id"),
                    sender_id=_require_text(sender_id, "sender_id"),
                    sender_name=sender_name.strip() or sender_id,
                    content=_require_text(content, "content"),
                    created_at=created_at,
                ),
            ),
        )
        return True

    def handle_group_command(
        self,
        *,
        sender_id: str,
        sender_name: str,
        group_id: str,
        text: str,
    ) -> str | None:
        summarize_match = _SUMMARIZE_PATTERN.fullmatch(text)
        confirm_match = _CONFIRM_PATTERN.fullmatch(text)
        complete_match = _COMPLETE_PATTERN.fullmatch(text)
        revise_match = _REVISE_PATTERN.fullmatch(text)
        if (
            summarize_match is None
            and confirm_match is None
            and complete_match is None
            and revise_match is None
        ):
            return None

        draft = self._transcript_store.get_draft(group_id=group_id)
        ticket_code = summarize_match.group(1) if summarize_match is not None else None
        interaction, error = self._resolve_context(
            group_id=group_id,
            ticket_code=ticket_code or (draft.ticket_code if draft else None),
        )
        if interaction is None:
            return error
        if not self.can_manage(interaction=interaction, sender_id=sender_id):
            return "只有该业务板块负责人或项目管理员可以总结或确认结案。"

        if complete_match is not None:
            return self._complete_and_publish(
                interaction=interaction,
                sender_id=sender_id,
                sender_name=sender_name,
                group_id=group_id,
            )

        if summarize_match is not None:
            saved_messages = self._sync_transcript(
                interaction=interaction,
                group_id=group_id,
            )
            if not saved_messages:
                return "暂时没有拉取到可用于结案的群聊记录，请先在群内完成沟通。"
            generated = self._summarizer.summarize(
                interaction=interaction,
                messages=saved_messages,
                authoritative_sender_ids=self._authoritative_sender_ids(
                    interaction
                ),
            )
            self._transcript_store.save_draft(generated)
            return _draft_preview(generated)

        if draft is None or draft.ticket_code != interaction.ticket_code:
            return "当前群还没有待确认的结案草稿，请先发送“总结并结案”。"
        if revise_match is not None:
            revised = CollaborationDraft(
                ticket_code=draft.ticket_code,
                group_id=draft.group_id,
                final_answer=revise_match.group(1).strip(),
                root_cause=draft.root_cause,
                knowledge_candidate=revise_match.group(1).strip(),
                created_at=int(time.time()),
            )
            self._transcript_store.save_draft(revised)
            return _draft_preview(revised)

        owner_name = sender_name.strip() or "部门负责人"
        claimed = self._state.claim_interaction(
            ticket_code=interaction.ticket_code,
            owner=owner_name,
            owner_id=sender_id,
        )
        if claimed is None:
            return "该问题状态已经变化，无法重复结案，请重新查看。"
        resolved = self._state.resolve_interaction(
            ticket_code=interaction.ticket_code,
            owner=claimed.owner or owner_name,
            final_answer=draft.final_answer,
            reviewed_by=owner_name,
            knowledge_status="approved",
        )
        if resolved is None:
            return "结案保存失败，问题仍保持待处理状态，请稍后重试。"
        return (
            f"问题 {interaction.ticket_code} 已结案。最终答复已保存，"
            "并进入可发布候选知识；发布到正式知识库前仍会执行自动检查。"
        )

    def _complete_and_publish(
        self,
        *,
        interaction: DingTalkInteraction,
        sender_id: str,
        sender_name: str,
        group_id: str,
    ) -> str:
        if self._knowledge_publisher is None:
            return "正式知识库发布尚未配置，问题保持沟通中，请联系项目管理员。"
        saved_messages = self._sync_transcript(
            interaction=interaction,
            group_id=group_id,
        )
        authoritative_ids = self._authoritative_sender_ids(interaction)
        authoritative_messages = tuple(
            message
            for message in saved_messages
            if message.sender_id in authoritative_ids
            and _is_authoritative_answer_candidate(message.content)
        )
        if not authoritative_messages:
            return (
                "还没有识别到负责人或项目管理员给出的明确回复，"
                "请先在群内完成答复再沉淀。"
            )
        generated = self._summarizer.summarize(
            interaction=interaction,
            messages=saved_messages,
            authoritative_sender_ids=authoritative_ids,
        )
        blocked_reason = _publication_block_reason(generated)
        if blocked_reason:
            self._transcript_store.save_draft(generated)
            return f"暂未入库：{blocked_reason}。问题保持沟通中。"

        owner_name = sender_name.strip() or "部门负责人"
        claimed = self._state.claim_interaction(
            ticket_code=interaction.ticket_code,
            owner=owner_name,
            owner_id=sender_id,
        )
        if claimed is None:
            return "该问题状态已经变化，无法重复结案，请重新查看。"
        try:
            published = self._knowledge_publisher.publish(
                interaction=interaction,
                draft=generated,
            )
        except Exception as exc:
            return (
                "正式知识库写入失败，问题没有结案，请稍后重试。"
                f"（{type(exc).__name__}）"
            )
        resolved = self._state.resolve_interaction(
            ticket_code=interaction.ticket_code,
            owner=claimed.owner or owner_name,
            final_answer=generated.final_answer,
            reviewed_by=owner_name,
            knowledge_status="approved",
        )
        if resolved is None:
            return (
                "知识已写入，但工单状态保存失败，请项目管理员人工核对，"
                f"知识文档编号：{published.document_id}。"
            )
        self._transcript_store.save_draft(generated)
        return (
            f"问题 {interaction.ticket_code} 已结案并已沉淀到正式知识库。"
            f"知识文档编号：{published.document_id}。"
        )

    def _sync_transcript(
        self,
        *,
        interaction: DingTalkInteraction,
        group_id: str,
    ) -> tuple[CollaborationMessage, ...]:
        local_messages = self._transcript_store.list_messages(
            ticket_code=interaction.ticket_code
        )
        try:
            remote_messages = self._gateway.list_messages(
                group_id=group_id,
                since=interaction.created_at - 60,
            )
        except CollaborationGroupError:
            if local_messages:
                return local_messages
            raise
        self._transcript_store.save_messages(
            ticket_code=interaction.ticket_code,
            group_id=group_id,
            messages=remote_messages,
        )
        return self._transcript_store.list_messages(
            ticket_code=interaction.ticket_code
        )

    def _authoritative_sender_ids(
        self,
        interaction: DingTalkInteraction,
    ) -> frozenset[str]:
        owner_id = self._handler_user_ids_by_domain.get(interaction.domain)
        configured_user_ids = tuple(
            user_id
            for user_id in (*self._operator_user_ids, owner_id)
            if user_id
        )
        return frozenset(
            sender_id
            for user_id in configured_user_ids
            for sender_id in (
                user_id,
                self._sender_aliases_by_user_id.get(user_id),
            )
            if sender_id
        )

    def _resolve_context(
        self,
        *,
        group_id: str,
        ticket_code: str | None,
    ) -> tuple[DingTalkInteraction | None, str]:
        active = tuple(
            interaction
            for interaction in self._state.list_interactions(status="needs_human")
            if interaction.collaboration_group_id == group_id
            and interaction.collaboration_status == "active"
        )
        if ticket_code is not None:
            normalized = ticket_code.upper()
            selected = next(
                (item for item in active if item.ticket_code == normalized),
                None,
            )
            if selected is None:
                return None, f"当前群没有待处理问题 {normalized}。"
            return selected, ""
        if len(active) == 1:
            return active[0], ""
        if not active:
            return None, "当前群没有待结案的问题。"
        details = "\n".join(
            f"- {item.ticket_code}：{item.question[:60]}" for item in active[-5:]
        )
        return (
            None,
            "当前群有多个待处理问题，请指定要结案的问题编号：\n"
            f"{details}\n\n例如：总结并结案 {active[-1].ticket_code}",
        )


def _draft_preview(draft: CollaborationDraft) -> str:
    return (
        f"【结案草稿】{draft.ticket_code}\n"
        f"给员工的最终答复：{draft.final_answer}\n\n"
        f"问题成因：{draft.root_cause}\n\n"
        f"候选知识：{draft.knowledge_candidate}\n\n"
        "请负责人核对后回复“确认结案”；需要改答案时回复"
        "“修改结案：核实后的完整答案”。"
    )


def _publication_block_reason(draft: CollaborationDraft) -> str | None:
    fields = (draft.final_answer, draft.knowledge_candidate)
    if any("仍需负责人补充" in value for value in fields):
        return "负责人结论仍不完整"
    if any(len(value.strip()) < 6 for value in fields):
        return "提炼出的最终答复过短，无法形成可靠知识"
    return None


def _is_authoritative_answer_candidate(content: str) -> bool:
    normalized = content.strip()
    if not normalized:
        return False
    lowered = normalized.casefold()
    if any(marker.casefold() in lowered for marker in _NON_BUSINESS_MARKERS):
        return False
    if (
        _SUMMARIZE_PATTERN.fullmatch(normalized)
        or _CONFIRM_PATTERN.fullmatch(normalized)
        or _COMPLETE_PATTERN.fullmatch(normalized)
        or _REVISE_PATTERN.fullmatch(normalized)
    ):
        return False
    without_mentions = re.sub(r"^(?:@\S+\s*)+", "", normalized).strip()
    compact = re.sub(r"[\s，。！？、,.!?：:；;]+", "", without_mentions)
    return len(compact) >= 6 and compact.casefold() not in _NON_ANSWER_TEXTS


def _parse_json_object(value: str) -> dict[str, object]:
    normalized = value.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", normalized)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise CollaborationGroupError(
            "The collaboration summary did not return valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise CollaborationGroupError(
            "The collaboration summary returned an invalid object."
        )
    return payload


def _required_payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CollaborationGroupError(
            f"The collaboration summary omitted {key}."
        )
    return value.strip()


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required.")
    return value.strip()
