from dataclasses import dataclass
from typing import Literal, Protocol

from app.dify_chat_client import DifyChatResponse
from app.enterprise_policy import (
    BusinessDomain,
    ChatChannel,
    EnterprisePolicy,
    PolicyAction,
    PolicyReason,
)
from app.handoff_policy import parse_dify_answer
from app.store_directory import (
    StoreDirectoryError,
    StoreDirectoryProvider,
    format_store_answer,
    parse_store_lookup,
)


class AssistantBrainProvider(Protocol):
    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse: ...


@dataclass(frozen=True)
class DialoguePlan:
    action: Literal["continue", "clarify"]
    question: str = ""


class AssistantDialoguePlanner(Protocol):
    def plan(
        self,
        *,
        question: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DialoguePlan: ...


class AssistantBusinessDialoguePlanner(Protocol):
    def plan(
        self,
        *,
        question: str,
        domain: BusinessDomain,
        user: str,
        conversation_id: str | None = None,
    ) -> DialoguePlan: ...


@dataclass(frozen=True)
class GeneralConversationReply:
    action: Literal["allow", "clarify"]
    text: str


class AssistantGeneralConversation(Protocol):
    def respond(
        self,
        *,
        message: str,
        user: str,
        conversation_id: str | None = None,
    ) -> GeneralConversationReply: ...


@dataclass(frozen=True)
class EnterpriseAnswer:
    domain: BusinessDomain
    action: PolicyAction
    text: str
    reason: PolicyReason | None = None
    conversation_id: str | None = None


class EnterpriseAssistant:
    """钉钉等消息入口与现有 Dify 问答能力之间的业务安全网关。"""

    _SOURCE_LABELS: dict[BusinessDomain, dict[str, str]] = {
        "hr": {
            "enterprise-knowledge-hr.md": "人事知识库（内部测试版）",
        },
        "shareholder": {
            "enterprise-knowledge-shareholder.md": "股东知识库（内部测试版）",
        },
        "activity": {
            "enterprise-knowledge-activity.md": "活动知识库（内部测试版）",
        },
        "finance": {
            "enterprise-knowledge-finance.md": "财务知识库（内部测试版）",
        },
    }
    _SOURCE_DOMAINS: dict[str, BusinessDomain] = {
        document_name: domain
        for domain, documents in _SOURCE_LABELS.items()
        for document_name in documents
    }
    _RESTRICTED_REASONS = frozenset(
        {
            "sensitive_financial_data",
            "prohibited_financial_action",
            "unauthorized_financial_data",
            "controlled_financial_data",
            "sensitive_personal_data",
            "sensitive_credentials",
            "prohibited_hr_action",
        }
    )
    _DENY_TEXT = (
        "该请求涉及敏感信息或受限操作，AI 不能直接提供或执行。"
        "请通过公司正式流程联系对应负责人。"
    )
    _HANDOFF_TEXT = (
        "现有知识不足以安全确认该问题，需要联系对应负责人核实。"
        "确认前不会承诺规则、金额或处理结果。"
    )
    _CLARIFY_TEXT = (
        "为了准确回答，请补充所在城市、人员角色、适用月份或具体业务场景。"
    )
    _QUESTION_CLARIFY_TEXT = (
        "我还不知道你具体想问哪件事。请补充关键词，"
        "例如入股、持股、分红、风险金或 OA 流程。"
    )
    _GREETING_TEXT = (
        "你好，我是伸活内部服务助手。目前主要回答股东制3.0相关的已审核问题。"
        "你可以直接问入股、持股、分红、风险金或 OA 流程。"
    )
    _CAPABILITIES_TEXT = (
        "目前我主要回答股东制3.0中已经审核的规则，包括股东类型、持股上限、"
        "入股 OA、分红、风险金和亏损处理。涉及个人金额、收款账户、审批执行"
        "或尚未确认的规则时，我会提示联系负责人，不会自行猜测。"
    )
    _THANKS_TEXT = "不客气。你可以继续问股东制3.0相关问题。"
    _FAREWELL_TEXT = "好的，有需要时再来问我。"
    _OUT_OF_SCOPE_TEXT = (
        "该问题不属于公司内部服务助手的业务范围，我不能提供相关内容。"
        "你可以咨询股东制3.0、入股、分红、风险金或 OA 流程等工作问题。"
    )
    _STORE_DIRECTORY_UNAVAILABLE_TEXT = (
        "已经识别到这是门店实时信息查询，但当前无法安全读取门店信息宽表。"
        "请稍后重试或联系门店运营负责人核实。"
    )
    _STORE_NOT_FOUND_TEXT = (
        "暂时没有匹配到唯一的门店，请补充钉钉门店宽表中的准确门店名称。"
    )

    def __init__(
        self,
        *,
        dify_client: AssistantBrainProvider | None = None,
        brain: AssistantBrainProvider | None = None,
        shareholder_brain: AssistantBrainProvider | None = None,
        shareholder_dialogue_planner: AssistantDialoguePlanner | None = None,
        business_dialogue_planner: AssistantBusinessDialoguePlanner | None = None,
        general_conversation: AssistantGeneralConversation | None = None,
        policy: EnterprisePolicy | None = None,
        store_directory: StoreDirectoryProvider | None = None,
    ) -> None:
        if dify_client is not None and brain is not None:
            raise ValueError("Provide either brain or dify_client, not both.")
        selected_brain = brain if brain is not None else dify_client
        if selected_brain is None:
            raise ValueError("brain is required.")
        self._brain = selected_brain
        self._shareholder_brain = shareholder_brain
        self._shareholder_dialogue_planner = shareholder_dialogue_planner
        self._business_dialogue_planner = business_dialogue_planner
        self._general_conversation = general_conversation
        self._policy = policy or EnterprisePolicy()
        self._store_directory = store_directory

    def answer(
        self,
        question: str,
        *,
        user: str,
        conversation_id: str | None = None,
        domain_hint: BusinessDomain | None = None,
        prior_question: str | None = None,
        channel: ChatChannel = "group",
    ) -> EnterpriseAnswer:
        normalized_question = question.strip()
        normalized_user = user.strip()
        if not normalized_question:
            raise ValueError("question is required.")
        if not normalized_user:
            raise ValueError("user is required.")
        normalized_prior_question = (
            prior_question.strip() if prior_question is not None else ""
        )

        has_prior_domain = domain_hint is not None and domain_hint != "unknown"
        conversation_reply = self._conversation_reply(
            normalized_question,
            has_prior_domain=has_prior_domain,
        )
        if conversation_reply is not None:
            action, text = conversation_reply
            return EnterpriseAnswer(
                domain="unknown",
                action=action,
                text=text,
                reason=(
                    "missing_business_context"
                    if action == "clarify"
                    else None
                ),
                conversation_id=conversation_id,
            )

        decision = self._policy.evaluate(normalized_question, channel=channel)
        if (
            channel == "direct"
            and self._general_conversation is not None
            and decision.domain == "unknown"
            and decision.action != "deny"
            and decision.reason not in self._RESTRICTED_REASONS
        ):
            general_reply = self._general_conversation.respond(
                message=normalized_question,
                user=normalized_user,
                conversation_id=conversation_id,
            )
            return EnterpriseAnswer(
                domain="unknown",
                action=general_reply.action,
                text=general_reply.text,
                reason=(
                    "missing_business_context"
                    if general_reply.action == "clarify"
                    else None
                ),
                conversation_id=conversation_id,
            )
        is_contextual_follow_up = (
            has_prior_domain
            and bool(conversation_id)
            and decision.domain == "unknown"
            and decision.action in ("allow", "handoff")
            and decision.reason in (None, "knowledge_gap")
            and self._looks_like_follow_up(normalized_question)
        )
        is_clarification_answer = (
            has_prior_domain
            and (
                decision.action == "allow"
                or (
                    decision.action == "handoff"
                    and decision.reason == "knowledge_gap"
                )
            )
            and self._looks_like_context_answer(normalized_question)
        )
        is_pending_clarification_answer = (
            bool(normalized_prior_question)
            and has_prior_domain
            and (
                decision.action == "allow"
                or (
                    decision.action == "handoff"
                    and decision.reason == "knowledge_gap"
                )
            )
        )
        uses_prior_domain = (
            is_contextual_follow_up
            or is_clarification_answer
            or is_pending_clarification_answer
        )
        if decision.action != "allow" and not uses_prior_domain:
            return EnterpriseAnswer(
                domain=decision.domain,
                action=decision.action,
                text=(
                    self._OUT_OF_SCOPE_TEXT
                    if decision.reason in (
                        "out_of_scope_topic",
                        "out_of_scope_sensitive_topic",
                    )
                    else (
                        self._DENY_TEXT
                        if decision.action == "deny"
                        or decision.reason in self._RESTRICTED_REASONS
                        else (
                            self._CLARIFY_TEXT
                            if decision.action == "clarify"
                            else self._HANDOFF_TEXT
                        )
                    )
                ),
                reason=decision.reason,
            )

        answer_domain = domain_hint if uses_prior_domain else decision.domain
        store_request = (
            parse_store_lookup(normalized_question)
            if answer_domain == "operations"
            else None
        )
        if store_request is not None:
            if self._store_directory is None:
                return EnterpriseAnswer(
                    domain="operations",
                    action="handoff",
                    text=self._STORE_DIRECTORY_UNAVAILABLE_TEXT,
                    reason="knowledge_gap",
                )
            try:
                store_record = self._store_directory.lookup(
                    store_name=store_request.store_name,
                    fields=store_request.fields,
                )
            except StoreDirectoryError:
                return EnterpriseAnswer(
                    domain="operations",
                    action="handoff",
                    text=self._STORE_DIRECTORY_UNAVAILABLE_TEXT,
                    reason="knowledge_gap",
                )
            if store_record is None:
                return EnterpriseAnswer(
                    domain="operations",
                    action="clarify",
                    text=self._STORE_NOT_FOUND_TEXT,
                    reason="missing_business_context",
                )
            return EnterpriseAnswer(
                domain="operations",
                action="allow",
                text=format_store_answer(store_record, store_request.fields),
            )

        dialogue_plan = None
        if channel == "direct" and not normalized_prior_question:
            if (
                answer_domain == "shareholder"
                and self._shareholder_dialogue_planner is not None
            ):
                dialogue_plan = self._shareholder_dialogue_planner.plan(
                    question=normalized_question,
                    user=normalized_user,
                    conversation_id=conversation_id,
                )
            elif (
                answer_domain != "unknown"
                and self._business_dialogue_planner is not None
            ):
                dialogue_plan = self._business_dialogue_planner.plan(
                    question=normalized_question,
                    domain=answer_domain,
                    user=normalized_user,
                    conversation_id=conversation_id,
                )
        if dialogue_plan is not None:
            if dialogue_plan.action == "clarify":
                return EnterpriseAnswer(
                    domain=answer_domain,
                    action="clarify",
                    text=dialogue_plan.question,
                    reason="missing_business_context",
                    conversation_id=conversation_id,
                )

        retrieval_question = normalized_question
        if normalized_prior_question:
            retrieval_question = (
                f"员工最初问题：{normalized_prior_question}\n"
                f"员工补充信息：{normalized_question}"
            )

        selected_brain = (
            self._shareholder_brain
            if answer_domain == "shareholder" and self._shareholder_brain is not None
            else self._brain
        )
        response = selected_brain.send_message(
            query=self._expand_retrieval_query(
                retrieval_question,
                domain=answer_domain,
            ),
            user=normalized_user,
            conversation_id=conversation_id,
        )
        parsed = parse_dify_answer(response.answer)
        if parsed.needs_human:
            return EnterpriseAnswer(
                domain=answer_domain,
                action="handoff",
                text=parsed.customer_text,
                reason="knowledge_gap",
                conversation_id=response.conversation_id,
            )
        source_domain = self._domain_from_sources(response.retriever_resources)
        resolved_domain = (
            source_domain if source_domain != "unknown" else answer_domain
        )
        return EnterpriseAnswer(
            domain=resolved_domain,
            action="allow",
            text=self._append_sources(
                parsed.customer_text,
                resources=response.retriever_resources,
                domain=resolved_domain,
            ),
            conversation_id=response.conversation_id,
        )

    @classmethod
    def _domain_from_sources(
        cls,
        resources: tuple[dict[str, object], ...],
    ) -> BusinessDomain:
        domains: list[BusinessDomain] = []
        for resource in resources:
            document_name = resource.get("document_name")
            if not isinstance(document_name, str):
                continue
            domain = cls._SOURCE_DOMAINS.get(document_name.strip())
            if domain is not None and domain not in domains:
                domains.append(domain)
        return domains[0] if len(domains) == 1 else "unknown"

    @classmethod
    def _append_sources(
        cls,
        answer: str,
        *,
        resources: tuple[dict[str, object], ...],
        domain: BusinessDomain,
    ) -> str:
        allowed_sources = cls._SOURCE_LABELS.get(domain, {})
        labels: list[str] = []
        for resource in resources:
            document_name = resource.get("document_name")
            if not isinstance(document_name, str):
                continue
            label = allowed_sources.get(document_name.strip())
            if label and label not in labels:
                labels.append(label)
        if not labels:
            return answer
        return f"{answer}\n\n参考资料：{'、'.join(labels)}"

    @staticmethod
    def _looks_like_follow_up(question: str) -> bool:
        prefixes = (
            "那",
            "这个",
            "还需要",
            "还要",
            "然后",
            "具体",
            "需要什么",
            "要什么",
            "怎么",
            "哪些",
            "多久",
            "在哪里",
            "从哪里",
        )
        return question.startswith(prefixes)

    @staticmethod
    def _looks_like_context_answer(question: str) -> bool:
        remaining = question
        matched = False
        context_terms = (
            "深圳",
            "上海",
            "总部",
            "中级教练",
            "高级教练",
            "大师级教练",
            "教练",
            "族长",
            "股东",
            "员工",
            "实习生",
            "兼职",
            "全职",
            "弹性兼职",
            "本月",
            "上月",
            "当月",
        )
        for term in context_terms:
            if term in remaining:
                matched = True
            remaining = remaining.replace(term, "")
        return matched and not remaining.strip(" ，。；、,.!?！？0123456789年月日")

    @classmethod
    def _conversation_reply(
        cls,
        question: str,
        *,
        has_prior_domain: bool,
    ) -> tuple[PolicyAction, str] | None:
        normalized = "".join(question.lower().split()).strip(
            "，。；、,.!?！？~～"
        )
        greetings = frozenset(
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
        capabilities = frozenset(
            {
                "你是谁",
                "你是什么",
                "你是干嘛的",
                "你能干嘛",
                "你能做什么",
                "你会什么",
                "你可以回答什么",
                "你能回答什么问题",
                "怎么用你",
            }
        )
        thanks = frozenset({"谢谢", "谢谢你", "感谢", "感谢你", "辛苦了"})
        farewells = frozenset({"再见", "拜拜", "先这样", "没事了"})
        vague_questions = frozenset(
            {
                "这个怎么弄",
                "这个咋弄",
                "怎么搞",
                "咋办",
                "这个啥意思",
                "这是什么意思",
                "这个是什么意思",
                "怎么操作",
                "然后呢",
                "接下来呢",
            }
        )

        if normalized in greetings:
            return "allow", cls._GREETING_TEXT
        if normalized in capabilities:
            return "allow", cls._CAPABILITIES_TEXT
        if normalized in thanks:
            return "allow", cls._THANKS_TEXT
        if normalized in farewells:
            return "allow", cls._FAREWELL_TEXT
        if normalized in vague_questions and not has_prior_domain:
            return "clarify", cls._QUESTION_CLARIFY_TEXT
        return None

    @staticmethod
    def _expand_retrieval_query(
        question: str,
        *,
        domain: BusinessDomain,
    ) -> str:
        compact = "".join(question.lower().split())
        transfer_terms = (
            "转了钱",
            "转钱",
            "钱转过去",
            "打了款",
            "打款后",
        )
        timing_terms = (
            "开始分",
            "啥时候分",
            "什么时候分",
            "几月分",
            "哪个月分",
        )
        if (
            domain == "shareholder"
            and "分期" not in compact
            and any(term in compact for term in transfer_terms)
            and any(term in compact for term in timing_terms)
        ):
            return (
                f"{question}\n"
                "标准检索意图：转账后从哪个月开始计算分红？"
            )
        dividend_view_terms = (
            "在哪看",
            "哪里看",
            "从哪看",
            "在哪查",
            "哪里查",
            "怎么查",
        )
        if (
            domain == "shareholder"
            and "分红" in compact
            and any(term in compact for term in dividend_view_terms)
        ):
            return (
                f"{question}\n"
                "标准检索意图：分红数据从哪里查看？"
                "月度损益表和开店资本单在哪里查看？"
            )
        if domain == "shareholder" and any(
            term in compact
            for term in (
                "再开一家",
                "开下一家",
                "下一家店",
                "开第二家",
                "再开一店",
                "开分店",
            )
        ):
            return (
                f"{question}\n"
                "标准检索意图：管理股东开下一家店需要满足哪些通用条件？"
                "个人资格需要核对哪些数据？"
            )
        return question
