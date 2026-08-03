from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.dify_chat_client import DifyChatResponse
from app.enterprise_assistant import (
    AssistantBrainProvider,
    DialoguePlan,
    GeneralConversationReply,
)
from app.enterprise_policy import BusinessDomain
from app.handoff_policy import parse_dify_answer


LOGGER = logging.getLogger(__name__)
_APPROVED_SHAREHOLDER_DOCUMENT = "enterprise-knowledge-shareholder.md"
_HANDOFF_ANSWER = (
    "[HANDOFF]现有已审核的股东制 3.0 资料不足以安全确认该问题，"
    "请联系股东制度负责人核实。"
)
_THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"}
)
_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?%?")
_STORE_EXPANSION_TERMS = (
    "再开一家",
    "开下一家",
    "下一家店",
    "开第二家",
    "再开一店",
    "开分店",
)
_ROLE_TERMS = ("族长", "管理股东", "技术股东", "师傅股东", "股东")
_DOMAIN_LABELS: dict[BusinessDomain, str] = {
    "hr": "人事与员工服务",
    "shareholder": "股东制3.0",
    "activity": "活动与营销",
    "finance": "财务与薪酬",
    "operations": "门店运营",
    "unknown": "公司内部业务",
}


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class OpenClawShareholderBrain:
    """Use approved Dify evidence before asking an isolated OpenClaw agent."""

    def __init__(
        self,
        *,
        evidence_brain: AssistantBrainProvider,
        executable: str = "openclaw",
        agent_id: str = "shareholder-3-0",
        thinking: str = "low",
        timeout_seconds: int = 90,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        normalized_executable = executable.strip()
        normalized_agent_id = agent_id.strip()
        normalized_thinking = thinking.strip().lower()
        if not normalized_executable:
            raise ValueError("OpenClaw executable is required.")
        if not normalized_agent_id:
            raise ValueError("OpenClaw agent ID is required.")
        if normalized_thinking not in _THINKING_LEVELS:
            raise ValueError("OpenClaw thinking level is invalid.")
        if not 5 <= timeout_seconds <= 600:
            raise ValueError("OpenClaw timeout must be between 5 and 600 seconds.")
        self._evidence_brain = evidence_brain
        self._executable = normalized_executable
        self._agent_id = normalized_agent_id
        self._thinking = normalized_thinking
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse:
        evidence = self._evidence_brain.send_message(
            query=query,
            user=user,
            conversation_id=conversation_id,
        )
        if parse_dify_answer(evidence.answer).needs_human:
            return evidence
        if not self._has_approved_source(evidence.retriever_resources):
            return self._handoff(evidence)

        prompt = self._build_prompt(query=query, evidence=evidence.answer)
        command = [
            self._executable,
            "agent",
            "--agent",
            self._agent_id,
            "--message",
            prompt,
            "--session-id",
            self._session_id(user=user, conversation_id=evidence.conversation_id),
            "--thinking",
            self._thinking,
            "--timeout",
            str(self._timeout_seconds),
            "--json",
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            LOGGER.warning("OpenClaw shareholder agent was unavailable.")
            return evidence

        if completed.returncode != 0:
            LOGGER.warning(
                "OpenClaw shareholder agent exited with status %d.",
                completed.returncode,
            )
            return evidence

        reply = self._extract_reply(completed.stdout)
        if not self._is_safe_rewrite(
            reply,
            allowed_facts=f"{query}\n{evidence.answer}\n股东制 3.0",
        ):
            LOGGER.warning("OpenClaw shareholder output failed local validation.")
            return evidence
        return DifyChatResponse(
            answer=reply,
            conversation_id=evidence.conversation_id,
            retriever_resources=evidence.retriever_resources,
        )

    @staticmethod
    def _has_approved_source(resources: Sequence[Mapping[str, object]]) -> bool:
        return any(
            resource.get("document_name") == _APPROVED_SHAREHOLDER_DOCUMENT
            for resource in resources
        )

    @staticmethod
    def _handoff(evidence: DifyChatResponse) -> DifyChatResponse:
        return DifyChatResponse(
            answer=_HANDOFF_ANSWER,
            conversation_id=evidence.conversation_id,
            retriever_resources=(),
        )

    @staticmethod
    def _build_prompt(*, query: str, evidence: str) -> str:
        return (
            "你正在处理股东制 3.0 内部问答。只允许依据下面的已审核证据回答，"
            "不得新增规则、金额、比例、日期、人员或办理承诺。证据不足或存在冲突时，"
            f"只输出：{_HANDOFF_ANSWER}\n\n"
            f"<employee_question>\n{query.strip()}\n</employee_question>\n\n"
            f"<approved_evidence>\n{evidence.strip()}\n</approved_evidence>\n\n"
            "你的人设是一位温柔、耐心的女性内部服务同事。请先直接回答员工真正关心的"
            "结论，再补充必要说明；语言自然，不机械复述问题，不使用编号列表，不描述"
            "内部系统实现。通常控制在两段、250字以内，没有必要的内容不要展开，也不要"
            "一次索要多项补充材料。不得把员工给出的宽泛身份擅自推断成更具体的股东类型，"
            "例如不能把‘已经入股的股东’直接称为‘管理股东’。"
        )

    @staticmethod
    def _session_id(*, user: str, conversation_id: str) -> str:
        digest = hashlib.sha256(
            f"{user.strip()}\0{conversation_id.strip()}".encode("utf-8")
        ).hexdigest()
        return f"shareholder-{digest[:24]}"

    @classmethod
    def _extract_reply(cls, stdout: str) -> str:
        payload = cls._last_json_object(stdout)
        if payload is None:
            return ""
        payloads = payload.get("payloads")
        if isinstance(payloads, list):
            texts = [
                item.get("text", "").strip()
                for item in payloads
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if texts:
                return "\n".join(text for text in texts if text).strip()
        for key in ("answer", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _last_json_object(stdout: str) -> dict[str, Any] | None:
        decoder = json.JSONDecoder()
        objects: list[dict[str, Any]] = []
        for index, character in enumerate(stdout):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stdout[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                objects.append(value)
        response_objects = [
            value
            for value in objects
            if "payloads" in value
            or any(key in value for key in ("answer", "text", "message"))
        ]
        if response_objects:
            return response_objects[-1]
        return objects[-1] if objects else None

    @staticmethod
    def _is_safe_rewrite(reply: str, *, allowed_facts: str) -> bool:
        normalized_reply = reply.strip()
        if not normalized_reply or len(normalized_reply) > 800:
            return False
        if normalized_reply.startswith("[HANDOFF]"):
            return normalized_reply == _HANDOFF_ANSWER
        allowed_numbers = set(_NUMBER_PATTERN.findall(allowed_facts))
        reply_numbers = set(_NUMBER_PATTERN.findall(normalized_reply))
        return reply_numbers.issubset(allowed_numbers)


class OpenClawShareholderDialoguePlanner:
    """Ask one context question before retrieval when a scenario is ambiguous."""

    def __init__(
        self,
        *,
        executable: str = "openclaw",
        agent_id: str = "shareholder-3-0",
        thinking: str = "low",
        timeout_seconds: int = 30,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        normalized_executable = executable.strip()
        normalized_agent_id = agent_id.strip()
        normalized_thinking = thinking.strip().lower()
        if not normalized_executable:
            raise ValueError("OpenClaw executable is required.")
        if not normalized_agent_id:
            raise ValueError("OpenClaw agent ID is required.")
        if normalized_thinking not in _THINKING_LEVELS:
            raise ValueError("OpenClaw thinking level is invalid.")
        if not 5 <= timeout_seconds <= 120:
            raise ValueError(
                "OpenClaw dialogue timeout must be between 5 and 120 seconds."
            )
        self._executable = normalized_executable
        self._agent_id = normalized_agent_id
        self._thinking = normalized_thinking
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def plan(
        self,
        *,
        question: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DialoguePlan:
        normalized_question = question.strip()
        prompt = self._build_prompt(normalized_question)
        command = [
            self._executable,
            "agent",
            "--agent",
            self._agent_id,
            "--message",
            prompt,
            "--session-id",
            self._session_id(user=user, conversation_id=conversation_id),
            "--thinking",
            self._thinking,
            "--timeout",
            str(self._timeout_seconds),
            "--json",
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            LOGGER.warning("OpenClaw shareholder dialogue planner was unavailable.")
            return self._fallback_plan(normalized_question)
        if completed.returncode != 0:
            LOGGER.warning(
                "OpenClaw shareholder dialogue planner exited with status %d.",
                completed.returncode,
            )
            return self._fallback_plan(normalized_question)

        reply = OpenClawShareholderBrain._extract_reply(completed.stdout)
        plan = self._parse_plan(reply)
        if plan is None:
            return self._fallback_plan(normalized_question)
        if (
            plan.action == "continue"
            and self._requires_mandatory_clarification(normalized_question)
        ):
            LOGGER.warning(
                "OpenClaw tried to skip a mandatory shareholder clarification; "
                "the local policy overrode it."
            )
            return self._fallback_plan(normalized_question)
        return plan

    @staticmethod
    def _requires_mandatory_clarification(question: str) -> bool:
        compact = "".join(question.casefold().split())
        return (
            any(term in compact for term in _STORE_EXPANSION_TERMS)
            and not any(term in compact for term in _ROLE_TERMS)
        )

    @staticmethod
    def _build_prompt(question: str) -> str:
        return (
            "你是股东制3.0内部服务的对话引导员。请判断员工当前的问题是否已经足够清楚，"
            "能够检索制度并给出唯一、可靠的回答。你不回答制度事实。只有缺少的信息会"
            "实质改变答案时才追问；清晰的制度类问题、寒暄或知识库是否覆盖的问题不要"
            "追问。若需要追问，一次只问一个最关键问题，问题要短、具体、容易回答。\n"
            "只输出一行 JSON，格式必须是以下两种之一：\n"
            '{"action":"continue","question":""}\n'
            '{"action":"clarify","question":"一个简短问题"}\n'
            "常见需要澄清的情况包括：指代不明、缺少适用对象/身份/门店/时间范围，或"
            "员工要判断个人结果但没有给出必要条件。如果问题涉及开下一家店但没有说明"
            "当前身份，优先确认员工是族长，还是已经入股的股东。不要询问姓名、手机号、"
            "身份证、银行卡等敏感信息；不要为了显得周到而索要与答案无关的信息。\n\n"
            f"员工问题：{question}"
        )

    @staticmethod
    def _session_id(*, user: str, conversation_id: str | None) -> str:
        digest = hashlib.sha256(
            f"{user.strip()}\0{(conversation_id or 'direct').strip()}".encode(
                "utf-8"
            )
        ).hexdigest()
        return f"shareholder-dialogue-{digest[:20]}"

    @classmethod
    def _parse_plan(cls, reply: str) -> DialoguePlan | None:
        normalized = reply.strip()
        if normalized.startswith("```"):
            normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", normalized)
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        action = payload.get("action")
        question = payload.get("question", "")
        if action == "continue" and question in ("", None):
            return DialoguePlan(action="continue")
        if (
            action == "clarify"
            and isinstance(question, str)
            and 2 <= len(question.strip()) <= 80
            and not _NUMBER_PATTERN.search(question)
        ):
            return DialoguePlan(action="clarify", question=question.strip())
        return None

    @staticmethod
    def _fallback_plan(question: str) -> DialoguePlan:
        if OpenClawShareholderDialoguePlanner._requires_mandatory_clarification(
            question
        ):
            return DialoguePlan(
                action="clarify",
                question="想先确认一下，你目前是族长，还是已经入股的股东呢？",
            )
        return DialoguePlan(action="continue")


class OpenClawBusinessDialoguePlanner:
    """Ask one useful clarification before retrieving non-shareholder business facts."""

    def __init__(
        self,
        *,
        executable: str = "openclaw",
        agent_id: str = "internal-companion",
        thinking: str = "low",
        timeout_seconds: int = 30,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self._executable = executable.strip()
        self._agent_id = agent_id.strip()
        self._thinking = thinking.strip().lower()
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        if not self._executable or not self._agent_id:
            raise ValueError("OpenClaw business dialogue settings are incomplete.")
        if self._thinking not in _THINKING_LEVELS:
            raise ValueError("OpenClaw thinking level is invalid.")
        if not 5 <= timeout_seconds <= 120:
            raise ValueError(
                "OpenClaw business dialogue timeout must be between 5 and 120 seconds."
            )

    def plan(
        self,
        *,
        question: str,
        domain: BusinessDomain,
        user: str,
        conversation_id: str | None = None,
    ) -> DialoguePlan:
        normalized_question = question.strip()
        command = [
            self._executable,
            "agent",
            "--agent",
            self._agent_id,
            "--message",
            self._build_prompt(normalized_question, domain=domain),
            "--session-id",
            self._session_id(
                user=user,
                conversation_id=conversation_id,
                domain=domain,
            ),
            "--thinking",
            self._thinking,
            "--timeout",
            str(self._timeout_seconds),
            "--json",
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            LOGGER.warning("OpenClaw business dialogue planner was unavailable.")
            return DialoguePlan(action="continue")
        if completed.returncode != 0:
            LOGGER.warning(
                "OpenClaw business dialogue planner exited with status %d.",
                completed.returncode,
            )
            return DialoguePlan(action="continue")

        reply = OpenClawShareholderBrain._extract_reply(completed.stdout)
        plan = OpenClawShareholderDialoguePlanner._parse_plan(reply)
        return plan if plan is not None else DialoguePlan(action="continue")

    @staticmethod
    def _build_prompt(question: str, *, domain: BusinessDomain) -> str:
        return (
            f"你是伸活内部服务助手的对话引导员，当前问题属于"
            f"“{_DOMAIN_LABELS[domain]}”。你只判断问题是否足够清楚，不回答制度事实。"
            "只有缺失的信息会实质改变检索结果或负责人判断时才追问；不要因为措辞口语化、"
            "有错别字或不够正式就追问。若问题已明确，直接继续。若需要追问，一次只问一个"
            "最关键问题，问题要短、具体、容易回答。不要索要姓名、手机号、身份证、银行卡"
            "等敏感信息。\n"
            "只输出一行 JSON：\n"
            '{"action":"continue","question":""}\n'
            "或\n"
            '{"action":"clarify","question":"一个简短问题"}\n\n'
            f"员工问题：{question}"
        )

    @staticmethod
    def _session_id(
        *,
        user: str,
        conversation_id: str | None,
        domain: BusinessDomain,
    ) -> str:
        digest = hashlib.sha256(
            (
                f"{user.strip()}\0{(conversation_id or 'direct').strip()}"
                f"\0{domain}"
            ).encode("utf-8")
        ).hexdigest()
        return f"business-dialogue-{digest[:20]}"


class OpenClawGeneralConversation:
    """Handle ordinary direct conversation without creating business tickets."""

    def __init__(
        self,
        *,
        executable: str = "openclaw",
        agent_id: str = "internal-companion",
        thinking: str = "low",
        timeout_seconds: int = 30,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self._executable = executable.strip()
        self._agent_id = agent_id.strip()
        self._thinking = thinking.strip().lower()
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        if not self._executable or not self._agent_id:
            raise ValueError("OpenClaw general conversation settings are incomplete.")
        if self._thinking not in _THINKING_LEVELS:
            raise ValueError("OpenClaw thinking level is invalid.")
        if not 5 <= timeout_seconds <= 120:
            raise ValueError(
                "OpenClaw general conversation timeout must be between 5 and 120 seconds."
            )

    def respond(
        self,
        *,
        message: str,
        user: str,
        conversation_id: str | None = None,
    ) -> GeneralConversationReply:
        prompt = self._build_prompt(message.strip())
        command = [
            self._executable,
            "agent",
            "--agent",
            self._agent_id,
            "--message",
            prompt,
            "--session-id",
            self._session_id(user=user, conversation_id=conversation_id),
            "--thinking",
            self._thinking,
            "--timeout",
            str(self._timeout_seconds),
            "--json",
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            LOGGER.warning("OpenClaw general conversation agent was unavailable.")
            return self._fallback()
        if completed.returncode != 0:
            LOGGER.warning(
                "OpenClaw general conversation agent exited with status %d.",
                completed.returncode,
            )
            return self._fallback()

        reply = OpenClawShareholderBrain._extract_reply(completed.stdout)
        parsed = self._parse_reply(reply)
        return parsed if parsed is not None else self._fallback()

    @staticmethod
    def _build_prompt(message: str) -> str:
        return (
            "你是伸活内部服务助手里的日常对话伙伴。理解员工自然语言并正常交流，不要把"
            "寒暄、询问你是谁、询问龙虾在哪里、表达感谢或普通闲聊当成业务工单。可以回答"
            "通用常识和使用方法，但不要编造任何公司制度、个人数据、审批结果或实时信息。"
            "如果员工显然在说公司业务但对象不清楚，只追问一个最关键的问题。回答自然、"
            "简短，通常不超过120字。\n"
            "只输出一行 JSON：\n"
            '{"action":"answer","text":"直接回复"}\n'
            "或\n"
            '{"action":"clarify","text":"一个简短追问"}\n\n'
            f"员工消息：{message}"
        )

    @staticmethod
    def _parse_reply(reply: str) -> GeneralConversationReply | None:
        normalized = reply.strip()
        if normalized.startswith("```"):
            normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", normalized)
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        action = payload.get("action")
        text = payload.get("text")
        if action not in {"answer", "clarify"} or not isinstance(text, str):
            return None
        normalized_text = text.strip()
        if not 1 <= len(normalized_text) <= 300:
            return None
        return GeneralConversationReply(
            action="allow" if action == "answer" else "clarify",
            text=normalized_text,
        )

    @staticmethod
    def _session_id(*, user: str, conversation_id: str | None) -> str:
        digest = hashlib.sha256(
            f"{user.strip()}\0{(conversation_id or 'direct').strip()}".encode("utf-8")
        ).hexdigest()
        return f"internal-companion-{digest[:20]}"

    @staticmethod
    def _fallback() -> GeneralConversationReply:
        return GeneralConversationReply(
            action="allow",
            text="我在呀。你可以直接跟我说想了解什么，涉及公司业务时我会再帮你查知识库。",
        )


def build_openclaw_shareholder_brain(
    *,
    evidence_brain: AssistantBrainProvider,
    environ: Mapping[str, str],
) -> OpenClawShareholderBrain | None:
    enabled = environ.get("OPENCLAW_SHAREHOLDER_ENABLED", "false").strip().lower()
    if enabled in {"false", "0", "no", "off", ""}:
        return None
    if enabled not in {"true", "1", "yes", "on"}:
        raise ValueError("OPENCLAW_SHAREHOLDER_ENABLED must be true or false.")
    try:
        timeout_seconds = int(
            environ.get("OPENCLAW_SHAREHOLDER_TIMEOUT_SECONDS", "90").strip()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("OpenClaw shareholder configuration is invalid.") from exc
    return OpenClawShareholderBrain(
        evidence_brain=evidence_brain,
        executable=environ.get("OPENCLAW_EXECUTABLE", "openclaw"),
        agent_id=environ.get("OPENCLAW_SHAREHOLDER_AGENT_ID", "shareholder-3-0"),
        thinking=environ.get("OPENCLAW_SHAREHOLDER_THINKING", "low"),
        timeout_seconds=timeout_seconds,
    )


def build_openclaw_shareholder_dialogue_planner(
    *,
    environ: Mapping[str, str],
) -> OpenClawShareholderDialoguePlanner | None:
    enabled = environ.get("OPENCLAW_SHAREHOLDER_ENABLED", "false").strip().lower()
    if enabled in {"false", "0", "no", "off", ""}:
        return None
    if enabled not in {"true", "1", "yes", "on"}:
        raise ValueError("OPENCLAW_SHAREHOLDER_ENABLED must be true or false.")
    try:
        timeout_seconds = int(
            environ.get("OPENCLAW_DIALOGUE_TIMEOUT_SECONDS", "30").strip()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("OpenClaw dialogue configuration is invalid.") from exc
    return OpenClawShareholderDialoguePlanner(
        executable=environ.get("OPENCLAW_EXECUTABLE", "openclaw"),
        agent_id=environ.get("OPENCLAW_SHAREHOLDER_AGENT_ID", "shareholder-3-0"),
        thinking=environ.get("OPENCLAW_SHAREHOLDER_THINKING", "low"),
        timeout_seconds=timeout_seconds,
    )


def build_openclaw_business_dialogue_planner(
    *,
    environ: Mapping[str, str],
) -> OpenClawBusinessDialoguePlanner | None:
    enabled = environ.get(
        "OPENCLAW_BUSINESS_DIALOGUE_ENABLED",
        environ.get("OPENCLAW_GENERAL_CONVERSATION_ENABLED", "false"),
    ).strip().lower()
    if enabled in {"false", "0", "no", "off", ""}:
        return None
    if enabled not in {"true", "1", "yes", "on"}:
        raise ValueError(
            "OPENCLAW_BUSINESS_DIALOGUE_ENABLED must be true or false."
        )
    try:
        timeout_seconds = int(
            environ.get(
                "OPENCLAW_BUSINESS_DIALOGUE_TIMEOUT_SECONDS",
                environ.get("OPENCLAW_GENERAL_TIMEOUT_SECONDS", "30"),
            ).strip()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "OpenClaw business dialogue configuration is invalid."
        ) from exc
    return OpenClawBusinessDialoguePlanner(
        executable=environ.get("OPENCLAW_EXECUTABLE", "openclaw"),
        agent_id=environ.get(
            "OPENCLAW_BUSINESS_DIALOGUE_AGENT_ID",
            environ.get("OPENCLAW_GENERAL_AGENT_ID", "internal-companion"),
        ),
        thinking=environ.get(
            "OPENCLAW_BUSINESS_DIALOGUE_THINKING",
            environ.get("OPENCLAW_GENERAL_THINKING", "low"),
        ),
        timeout_seconds=timeout_seconds,
    )


def build_openclaw_general_conversation(
    *,
    environ: Mapping[str, str],
) -> OpenClawGeneralConversation | None:
    enabled = environ.get(
        "OPENCLAW_GENERAL_CONVERSATION_ENABLED", "false"
    ).strip().lower()
    if enabled in {"false", "0", "no", "off", ""}:
        return None
    if enabled not in {"true", "1", "yes", "on"}:
        raise ValueError(
            "OPENCLAW_GENERAL_CONVERSATION_ENABLED must be true or false."
        )
    try:
        timeout_seconds = int(
            environ.get("OPENCLAW_GENERAL_TIMEOUT_SECONDS", "30").strip()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("OpenClaw general conversation configuration is invalid.") from exc
    return OpenClawGeneralConversation(
        executable=environ.get("OPENCLAW_EXECUTABLE", "openclaw"),
        agent_id=environ.get(
            "OPENCLAW_GENERAL_AGENT_ID", "internal-companion"
        ),
        thinking=environ.get("OPENCLAW_GENERAL_THINKING", "low"),
        timeout_seconds=timeout_seconds,
    )
