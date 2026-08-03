"""Native DingTalk Stream entry point with durable per-conversation state."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dingtalk_stream import (
    AckMessage,
    ChatbotHandler,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)

from app.dingtalk_adapter import (
    DingTalkAssistantAdapter,
    DingTalkInboundMessage,
    DingTalkState,
    SQLiteDingTalkState,
)
from app.dingtalk_mvp_agent import build_brain, load_env_file
from app.dingtalk_messaging import (
    DingTalkMessagingError,
    DingTalkProactiveMessenger,
)
from app.dingtalk_collaboration import (
    CollaborationGroupError,
    DingTalkCollaborationService,
    DwsCollaborationGroupGateway,
)
from app.dingtalk_collaboration_closure import (
    BrainCollaborationSummarizer,
    DifyCollaborationKnowledgePublisher,
    DingTalkCollaborationClosureService,
    SQLiteCollaborationTranscriptStore,
)
from app.dingtalk_collaboration_cards import (
    DingTalkCollaborationCardHandler,
    DingTalkCollaborationCardService,
)
from app.dingtalk_work_orders import DingTalkWorkOrderService
from app.dify_client import DifyClient
from app.enterprise_assistant import EnterpriseAnswer, EnterpriseAssistant
from app.openclaw_brain import (
    build_openclaw_business_dialogue_planner,
    build_openclaw_general_conversation,
    build_openclaw_shareholder_brain,
    build_openclaw_shareholder_dialogue_planner,
)
from app.openclaw_project_operator import OpenClawProjectOperator
from app.store_directory import load_store_directory


LOGGER = logging.getLogger("dingtalk-stream")
_UNAVAILABLE_TEXT = "服务暂时不可用，请稍后重试或联系对应负责人。"
_TEXT_ONLY_TEXT = "目前只支持文字提问，请直接发送问题文字。"
_ROUTABLE_DOMAINS = frozenset(
    {"hr", "shareholder", "activity", "finance", "operations", "unknown"}
)


@dataclass(frozen=True)
class DingTalkStreamSettings:
    client_id: str
    client_secret: str = field(repr=False)
    allowed_user_ids: frozenset[str]
    handler_user_ids: frozenset[str]
    operator_user_ids: frozenset[str]
    domain_handler_user_ids: dict[str, str]
    robot_code: str
    state_database_path: Path
    auto_group_enabled: bool
    dws_executable: str
    dws_profile: str | None
    collaboration_group_prefix: str
    project_operator_enabled: bool

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "DingTalkStreamSettings":
        source = os.environ if environ is None else environ
        client_id = source.get("DINGTALK_CLIENT_ID", "").strip()
        client_secret = source.get("DINGTALK_CLIENT_SECRET", "").strip()
        allowed = frozenset(
            item.strip()
            for item in source.get("DINGTALK_ALLOWED_USERS", "").split(",")
            if item.strip()
        )
        handlers = frozenset(
            item.strip()
            for item in source.get("DINGTALK_HANDLER_USERS", "").split(",")
            if item.strip()
        )
        operators = frozenset(
            item.strip()
            for item in source.get("DINGTALK_OPERATOR_USERS", "").split(",")
            if item.strip()
        )
        domain_handlers = _parse_domain_handlers(
            source.get("DINGTALK_DOMAIN_HANDLERS", "")
        )
        robot_code = source.get("DINGTALK_ROBOT_CODE", client_id).strip()
        database_path = Path(
            source.get(
                "DINGTALK_STATE_DB_PATH",
                ".runtime/customer-service/dingtalk.db",
            ).strip()
        )
        auto_group_enabled = _parse_bool(
            source.get("DINGTALK_AUTO_GROUP_ENABLED", "false")
        )
        dws_executable = source.get("DINGTALK_DWS_EXECUTABLE", "dws").strip()
        dws_profile = source.get("DINGTALK_DWS_PROFILE", "").strip() or None
        collaboration_group_prefix = source.get(
            "DINGTALK_COLLABORATION_GROUP_PREFIX",
            "内部服务协作",
        ).strip()
        project_operator_enabled = _parse_bool(
            source.get("OPENCLAW_PROJECT_OPERATOR_ENABLED", "false")
        )
        if not client_id or not client_secret or not allowed or not robot_code:
            raise ValueError(
                "DingTalk Stream configuration requires credentials and an "
                "allowed user list."
            )
        if not str(database_path):
            raise ValueError("DingTalk state database path is required.")
        if not handlers.issubset(allowed):
            raise ValueError(
                "DingTalk work-order handlers must also be in the allowed user list."
            )
        if not operators.issubset(handlers):
            raise ValueError(
                "DingTalk project operators must also be allowlisted work-order handlers."
            )
        if not set(domain_handlers.values()).issubset(handlers):
            raise ValueError(
                "DingTalk department owners must also be allowlisted work-order handlers."
            )
        if project_operator_enabled and not operators:
            raise ValueError(
                "The OpenClaw project operator requires at least one configured "
                "DingTalk project operator."
            )
        if auto_group_enabled and not domain_handlers:
            raise ValueError(
                "DingTalk automatic groups require department owner routing."
            )
        if auto_group_enabled and (
            not dws_executable or not collaboration_group_prefix
        ):
            raise ValueError(
                "DingTalk automatic group command settings are incomplete."
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            allowed_user_ids=allowed,
            handler_user_ids=handlers,
            operator_user_ids=operators,
            domain_handler_user_ids=domain_handlers,
            robot_code=robot_code,
            state_database_path=database_path,
            auto_group_enabled=auto_group_enabled,
            dws_executable=dws_executable,
            dws_profile=dws_profile,
            collaboration_group_prefix=collaboration_group_prefix,
            project_operator_enabled=project_operator_enabled,
        )


class DingTalkEnterpriseHandler(ChatbotHandler):
    """Adapt SDK messages to the existing enterprise assistant gateway."""

    def __init__(
        self,
        *,
        adapter: DingTalkAssistantAdapter,
        allowed_user_ids: frozenset[str],
        state: DingTalkState | None = None,
        work_orders: DingTalkWorkOrderService | None = None,
        collaborations: DingTalkCollaborationService | None = None,
        collaboration_closure: DingTalkCollaborationClosureService | None = None,
        collaboration_cards: DingTalkCollaborationCardService | None = None,
        collaboration_card_handler: DingTalkCollaborationCardHandler | None = None,
        project_operator: OpenClawProjectOperator | None = None,
    ) -> None:
        super().__init__()
        self._adapter = adapter
        self._allowed_user_ids = allowed_user_ids
        self._state = state
        self._work_orders = work_orders
        self._collaborations = collaborations
        self._collaboration_closure = collaboration_closure
        self._collaboration_cards = collaboration_cards
        self._project_operator = project_operator
        self.card_callback_handler = collaboration_card_handler

    async def process(self, callback) -> tuple[int, str]:
        incoming = ChatbotMessage.from_dict(callback.data)
        sender_id = (incoming.sender_staff_id or incoming.sender_id or "").strip()
        if sender_id not in self._allowed_user_ids:
            LOGGER.warning(
                "ignored message from non-allowlisted sender message_id=%s",
                incoming.message_id,
            )
            return AckMessage.STATUS_OK, "ignored"
        if (
            self._state is not None
            and self._state.is_processed(incoming.message_id)
        ):
            return AckMessage.STATUS_OK, "duplicate"

        text_parts = incoming.get_text_list() or []
        normalized_text = "\n".join(
            part.strip() for part in text_parts if isinstance(part, str) and part.strip()
        ).strip()
        LOGGER.info(
            "received message_id=%s message_type=%r text_parts=%d",
            incoming.message_id,
            incoming.message_type,
            len(text_parts),
        )
        if not normalized_text:
            await asyncio.to_thread(self.reply_text, _TEXT_ONLY_TEXT, incoming)
            return AckMessage.STATUS_OK, "unsupported message type"

        channel = "group" if str(incoming.conversation_type) == "2" else "direct"
        if self._project_operator is not None:
            operator_reply = await asyncio.to_thread(
                self._project_operator.handle,
                sender_id=sender_id,
                text=normalized_text,
                is_direct=(channel == "direct"),
            )
            if operator_reply is not None:
                if self._state is not None:
                    self._state.mark_processed(incoming.message_id)
                await asyncio.to_thread(
                    self.reply_text,
                    operator_reply.text,
                    incoming,
                )
                return AckMessage.STATUS_OK, "project operator command"
        if channel == "group" and self._collaboration_closure is not None:
            try:
                closure_result = await asyncio.to_thread(
                    self._collaboration_closure.handle_group_command,
                    sender_id=sender_id,
                    sender_name=(incoming.sender_nick or "").strip(),
                    group_id=incoming.conversation_id,
                    text=normalized_text,
                )
            except CollaborationGroupError:
                LOGGER.exception(
                    "failed to summarize DingTalk collaboration group group=%s",
                    incoming.conversation_id,
                )
                closure_result = (
                    "群聊记录暂时无法生成可靠结案草稿，问题仍保持待处理状态。"
                    "请稍后重试。"
                )
            if closure_result is not None:
                if self._state is not None:
                    self._state.mark_processed(incoming.message_id)
                await asyncio.to_thread(self.reply_text, closure_result, incoming)
                return AckMessage.STATUS_OK, "collaboration closure command"
            if self._collaboration_closure.is_active_group(
                group_id=incoming.conversation_id
            ):
                recorded = await asyncio.to_thread(
                    self._collaboration_closure.record_group_message,
                    message_id=incoming.message_id,
                    sender_id=sender_id,
                    sender_name=(incoming.sender_nick or "").strip(),
                    group_id=incoming.conversation_id,
                    content=normalized_text,
                    created_at=_normalize_message_timestamp(incoming.create_at),
                )
                if not recorded:
                    LOGGER.warning(
                        "active collaboration group changed before message capture "
                        "message_id=%s group=%s",
                        incoming.message_id,
                        incoming.conversation_id,
                    )
                if self._state is not None:
                    self._state.mark_processed(incoming.message_id)
                LOGGER.info(
                    "collected collaboration message without creating a new ticket "
                    "message_id=%s group=%s",
                    incoming.message_id,
                    incoming.conversation_id,
                )
                return AckMessage.STATUS_OK, "collaboration message collected"
        if self._work_orders is not None:
            try:
                command = await asyncio.to_thread(
                    self._work_orders.handle_command,
                    sender_id=sender_id,
                    sender_name=(incoming.sender_nick or "").strip(),
                    text=normalized_text,
                    is_direct=(channel == "direct"),
                )
            except DingTalkMessagingError:
                LOGGER.exception(
                    "failed to deliver DingTalk work-order answer message_id=%s",
                    incoming.message_id,
                )
                await asyncio.to_thread(
                    self.reply_text,
                    "钉钉消息发送失败，工单没有关闭，请稍后重试。",
                    incoming,
                )
                return AckMessage.STATUS_OK, "work order delivery failed"
            if command is not None:
                if self._state is not None:
                    self._state.mark_processed(incoming.message_id)
                await asyncio.to_thread(self.reply_text, command.text, incoming)
                return AckMessage.STATUS_OK, "work order command"

        inbound = DingTalkInboundMessage(
            message_id=incoming.message_id,
            sender_id=sender_id,
            conversation_id=incoming.conversation_id,
            text=normalized_text,
            channel=channel,
            sender_is_bot=(
                bool(incoming.chatbot_user_id)
                and incoming.sender_id == incoming.chatbot_user_id
            ),
        )
        try:
            answer = await asyncio.to_thread(self._adapter.handle, inbound)
        except Exception:
            LOGGER.exception(
                "failed to answer DingTalk message message_id=%s",
                incoming.message_id,
            )
            fallback_answer = EnterpriseAnswer(
                domain="unknown",
                action="handoff",
                text=_UNAVAILABLE_TEXT,
                reason="system_unavailable",
            )
            try:
                answer = await asyncio.to_thread(
                    self._adapter.record_failure,
                    inbound,
                    fallback_answer,
                )
            except Exception:
                LOGGER.exception(
                    "failed to record DingTalk system failure message_id=%s",
                    incoming.message_id,
                )
                answer = fallback_answer

        if answer is not None:
            reply_text = answer.text
            if (
                answer.action == "handoff"
                and self._state is not None
                and self._work_orders is not None
            ):
                interaction = self._state.latest_needs_human(
                    sender_id=sender_id,
                    dingtalk_conversation_id=incoming.conversation_id,
                )
                if interaction is not None:
                    collaboration_opened = False
                    if self._collaborations is not None:
                        try:
                            result = await asyncio.to_thread(
                                self._collaborations.ensure_group,
                                interaction,
                            )
                            if (
                                not result.reused
                                and self._collaboration_cards is not None
                            ):
                                try:
                                    await asyncio.to_thread(
                                        self._collaboration_cards.send_open_card,
                                        interaction=interaction,
                                        group_id=result.group_id,
                                    )
                                except DingTalkMessagingError:
                                    LOGGER.exception(
                                        "failed to send collaboration action card "
                                        "ticket=%s",
                                        interaction.ticket_code,
                                    )
                            reply_text = result.employee_message
                            collaboration_opened = True
                        except CollaborationGroupError:
                            LOGGER.exception(
                                "failed to open DingTalk collaboration group "
                                "ticket=%s",
                                interaction.ticket_code,
                            )
                    if not collaboration_opened:
                        try:
                            await asyncio.to_thread(
                                self._work_orders.notify_handlers,
                                interaction,
                            )
                        except DingTalkMessagingError:
                            LOGGER.exception(
                                "failed to notify DingTalk work-order handlers "
                                "ticket=%s",
                                interaction.ticket_code,
                            )
            await asyncio.to_thread(self.reply_text, reply_text, incoming)
        return AckMessage.STATUS_OK, "OK"


def build_handler(
    environ: Mapping[str, str] | None = None,
) -> tuple[DingTalkStreamSettings, DingTalkEnterpriseHandler]:
    settings = DingTalkStreamSettings.from_env(environ)
    brain = build_brain(environ)
    source = os.environ if environ is None else environ
    state = SQLiteDingTalkState(settings.state_database_path)
    work_orders = None
    collaborations = None
    collaboration_closure = None
    collaboration_cards = None
    collaboration_card_handler = None
    project_operator = None
    if settings.project_operator_enabled:
        project_operator = OpenClawProjectOperator(
            operator_user_ids=settings.operator_user_ids,
            project_root=Path(__file__).resolve().parents[1],
        )
    if settings.handler_user_ids:
        messenger = DingTalkProactiveMessenger(
            app_key=settings.client_id,
            app_secret=settings.client_secret,
            robot_code=settings.robot_code,
        )
        work_orders = DingTalkWorkOrderService(
            state=state,
            messenger=messenger,
            handler_user_ids=settings.handler_user_ids,
            handler_user_ids_by_domain=settings.domain_handler_user_ids,
        )
        if settings.auto_group_enabled:
            group_gateway = DwsCollaborationGroupGateway(
                robot_code=settings.robot_code,
                executable=settings.dws_executable,
                profile=settings.dws_profile,
            )
            configured_authoritative_users = tuple(
                dict.fromkeys(
                    (
                        *settings.operator_user_ids,
                        *settings.domain_handler_user_ids.values(),
                    )
                )
            )
            try:
                sender_aliases = group_gateway.resolve_sender_aliases(
                    configured_authoritative_users
                )
            except CollaborationGroupError:
                LOGGER.exception(
                    "failed to resolve openDingTalkId aliases for collaboration "
                    "history; real-time userId messages remain available"
                )
                sender_aliases = {}
            collaborations = DingTalkCollaborationService(
                state=state,
                gateway=group_gateway,
                handler_user_ids_by_domain=settings.domain_handler_user_ids,
                operator_user_ids=settings.operator_user_ids,
                group_name_prefix=settings.collaboration_group_prefix,
            )
            collaboration_closure = DingTalkCollaborationClosureService(
                state=state,
                gateway=group_gateway,
                transcript_store=SQLiteCollaborationTranscriptStore(
                    settings.state_database_path
                ),
                summarizer=BrainCollaborationSummarizer(brain),
                handler_user_ids_by_domain=settings.domain_handler_user_ids,
                operator_user_ids=settings.operator_user_ids,
                sender_aliases_by_user_id=sender_aliases,
                knowledge_publisher=_build_collaboration_knowledge_publisher(
                    source
                ),
            )
            collaboration_cards = DingTalkCollaborationCardService(
                state=state,
                closure=collaboration_closure,
                messenger=messenger,
                operator_user_ids=settings.operator_user_ids,
            )
            collaboration_card_handler = DingTalkCollaborationCardHandler(
                service=collaboration_cards,
            )
    adapter = DingTalkAssistantAdapter(
        assistant=EnterpriseAssistant(
            brain=brain,
            shareholder_brain=build_openclaw_shareholder_brain(
                evidence_brain=brain,
                environ=source,
            ),
            shareholder_dialogue_planner=(
                build_openclaw_shareholder_dialogue_planner(
                    environ=source,
                )
            ),
            business_dialogue_planner=build_openclaw_business_dialogue_planner(
                environ=source,
            ),
            general_conversation=build_openclaw_general_conversation(
                environ=source,
            ),
            store_directory=load_store_directory(
                source
            ),
        ),
        state=state,
    )
    return settings, DingTalkEnterpriseHandler(
        adapter=adapter,
        allowed_user_ids=settings.allowed_user_ids,
        state=state,
        work_orders=work_orders,
        collaborations=collaborations,
        collaboration_closure=collaboration_closure,
        collaboration_cards=collaboration_cards,
        collaboration_card_handler=collaboration_card_handler,
        project_operator=project_operator,
    )


def _parse_domain_handlers(value: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        domain, separator, handler_id = item.partition("=")
        normalized_domain = domain.strip()
        normalized_handler = handler_id.strip()
        if (
            not separator
            or normalized_domain not in _ROUTABLE_DOMAINS
            or not normalized_handler
            or normalized_domain in assignments
        ):
            raise ValueError(
                "DINGTALK_DOMAIN_HANDLERS must contain unique domain=handler pairs."
            )
        assignments[normalized_domain] = normalized_handler
    return assignments


def _normalize_message_timestamp(value: object) -> int:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return int(time.time())
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return timestamp if timestamp > 0 else int(time.time())


def _build_collaboration_knowledge_publisher(
    environ: Mapping[str, str],
) -> DifyCollaborationKnowledgePublisher | None:
    base_url = environ.get("DIFY_BASE_URL", "").strip()
    dataset_id = environ.get("DIFY_DATASET_ID", "").strip()
    api_key = environ.get("DIFY_DATASET_API_KEY", "").strip()
    if not base_url or not dataset_id or not api_key:
        LOGGER.warning(
            "collaboration knowledge publishing is disabled because Dify "
            "dataset settings are incomplete"
        )
        return None
    return DifyCollaborationKnowledgePublisher(
        dataset_id=dataset_id,
        client=DifyClient(base_url=base_url, api_key=api_key),
        indexing_technique=environ.get(
            "DIFY_INDEXING_TECHNIQUE", "high_quality"
        ).strip()
        or "high_quality",
    )


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError("Boolean environment values must be true or false.")


def run(environ: Mapping[str, str] | None = None) -> None:
    load_env_file()
    source = os.environ if environ is None else environ
    settings, handler = build_handler(source)
    credential = Credential(settings.client_id, settings.client_secret)
    client = DingTalkStreamClient(credential)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)
    if handler.card_callback_handler is not None:
        client.register_callback_handler(
            handler.card_callback_handler.TOPIC_CARD_CALLBACK,
            handler.card_callback_handler,
        )
    LOGGER.info(
        "starting DingTalk Stream allowed_users=%d state_db=%s "
        "auto_group=%s cards=%s group_prefix=%r",
        len(settings.allowed_user_ids),
        settings.state_database_path,
        settings.auto_group_enabled,
        handler.card_callback_handler is not None,
        settings.collaboration_group_prefix,
    )
    client.start_forever()


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=os.environ.get("DINGTALK_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
