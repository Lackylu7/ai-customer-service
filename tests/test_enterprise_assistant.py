import unittest

from app.dify_chat_client import DifyChatResponse
from app.enterprise_assistant import (
    DialoguePlan,
    EnterpriseAssistant,
    GeneralConversationReply,
)
from app.store_directory import StoreRecord


class FakeDifyClient:
    def __init__(
        self,
        answer: str = "请在工作台的 OA 审批中发起报销。",
        resources: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.calls: list[dict[str, str | None]] = []
        self.answer = answer
        self.resources = resources

    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse:
        self.calls.append(
            {
                "query": query,
                "user": user,
                "conversation_id": conversation_id,
            }
        )
        return DifyChatResponse(
            answer=self.answer,
            conversation_id="dify-conversation-1",
            retriever_resources=self.resources,
        )


class FakeStoreDirectory:
    def __init__(self, record: StoreRecord | None) -> None:
        self.record = record
        self.calls = []

    def lookup(self, *, store_name, fields):
        self.calls.append({"store_name": store_name, "fields": tuple(fields)})
        return self.record


class RecordingDialoguePlanner:
    def __init__(self, plan: DialoguePlan) -> None:
        self.result = plan
        self.calls = []

    def plan(self, *, question, user, conversation_id=None):
        self.calls.append(
            {
                "question": question,
                "user": user,
                "conversation_id": conversation_id,
            }
        )
        return self.result


class RecordingBusinessDialoguePlanner:
    def __init__(self, plan: DialoguePlan) -> None:
        self.result = plan
        self.calls = []

    def plan(self, *, question, domain, user, conversation_id=None):
        self.calls.append(
            {
                "question": question,
                "domain": domain,
                "user": user,
                "conversation_id": conversation_id,
            }
        )
        return self.result


class RecordingGeneralConversation:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def respond(self, *, message, user, conversation_id=None):
        self.calls.append(
            {"message": message, "user": user, "conversation_id": conversation_id}
        )
        return self.reply


class EnterpriseAssistantTests(unittest.TestCase):
    def test_unknown_direct_conversation_uses_openclaw_without_creating_handoff(self):
        brain = FakeDifyClient()
        conversation = RecordingGeneralConversation(
            GeneralConversationReply(
                action="allow",
                text="我就在这里呀，现在这个助手已经接入龙虾了。",
            )
        )
        assistant = EnterpriseAssistant(
            brain=brain,
            general_conversation=conversation,
        )

        result = assistant.answer(
            "龙虾在哪",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertIn("就在这里", result.text)
        self.assertEqual(len(conversation.calls), 1)
        self.assertEqual(brain.calls, [])

    def test_shareholder_dialogue_planner_can_clarify_before_retrieval(self) -> None:
        brain = FakeDifyClient()
        planner = RecordingDialoguePlanner(
            DialoguePlan(
                action="clarify",
                question="想先确认一下，你目前是族长，还是已经入股的股东呢？",
            )
        )
        assistant = EnterpriseAssistant(
            brain=brain,
            shareholder_dialogue_planner=planner,
        )

        result = assistant.answer(
            "我要开下一家店，需要达到哪些条件？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "clarify")
        self.assertEqual(result.domain, "shareholder")
        self.assertIn("族长", result.text)
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(brain.calls, [])

    def test_business_dialogue_planner_can_clarify_finance_question(self) -> None:
        brain = FakeDifyClient()
        planner = RecordingBusinessDialoguePlanner(
            DialoguePlan(
                action="clarify",
                question="想确认一下，你说的是哪个月份、哪一项薪酬呢？",
            )
        )
        assistant = EnterpriseAssistant(
            brain=brain,
            business_dialogue_planner=planner,
        )

        result = assistant.answer(
            "我的工资好像不对。",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "clarify")
        self.assertEqual(result.domain, "finance")
        self.assertEqual(planner.calls[0]["domain"], "finance")
        self.assertEqual(brain.calls, [])

    def test_clarification_context_is_combined_with_original_question(self) -> None:
        brain = FakeDifyClient("需要结合族长身份核对开店条件。")
        planner = RecordingDialoguePlanner(DialoguePlan(action="continue"))
        assistant = EnterpriseAssistant(
            brain=brain,
            shareholder_dialogue_planner=planner,
        )

        result = assistant.answer(
            "我是族长。",
            user="dingtalk:user-1",
            domain_hint="shareholder",
            prior_question="我要开下一家店，需要达到哪些条件？",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertIn("员工最初问题", brain.calls[0]["query"])
        self.assertIn("我是族长", brain.calls[0]["query"])
        self.assertEqual(planner.calls, [])

    def test_safe_store_query_uses_live_directory_without_calling_brain(self) -> None:
        brain = FakeDifyClient()
        store_directory = FakeStoreDirectory(
            StoreRecord(
                store_name="会展中心",
                values={"opening_date": "2023-06-05"},
            )
        )
        assistant = EnterpriseAssistant(
            brain=brain,
            store_directory=store_directory,
        )

        result = assistant.answer(
            "会展中心店什么时候开业？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "operations")
        self.assertIn("2023-06-05", result.text)
        self.assertIn("实时只读", result.text)
        self.assertEqual(brain.calls, [])
        self.assertEqual(
            store_directory.calls,
            [{"store_name": "会展中心", "fields": ("opening_date",)}],
        )

    def test_store_query_does_not_fall_back_to_dify_when_directory_is_off(self) -> None:
        brain = FakeDifyClient()
        assistant = EnterpriseAssistant(brain=brain)

        result = assistant.answer(
            "会展中心店什么时候开业？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "handoff")
        self.assertEqual(result.domain, "operations")
        self.assertEqual(brain.calls, [])

    def test_shareholder_new_store_question_does_not_use_store_directory(self) -> None:
        brain = FakeDifyClient(answer="25 万元只是新店预估金额。")
        store_directory = FakeStoreDirectory(None)
        assistant = EnterpriseAssistant(
            brain=brain,
            store_directory=store_directory,
        )

        result = assistant.answer(
            "新店入股是不是固定25万元？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "shareholder")
        self.assertEqual(result.text, "25 万元只是新店预估金额。")
        self.assertEqual(store_directory.calls, [])
        self.assertEqual(len(brain.calls), 1)

    def test_shareholder_store_transfer_question_does_not_use_store_directory(
        self,
    ) -> None:
        brain = FakeDifyClient(
            answer="[HANDOFF]现有资料无法确认跨城市调店后的股东权益，请人工核实。"
        )
        store_directory = FakeStoreDirectory(None)
        assistant = EnterpriseAssistant(
            brain=brain,
            store_directory=store_directory,
        )

        result = assistant.answer(
            "股东制3.0里，如果股东跨城市调店，原门店的风险金和分红资格具体如何处理？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "handoff")
        self.assertEqual(result.domain, "shareholder")
        self.assertEqual(result.reason, "knowledge_gap")
        self.assertEqual(store_directory.calls, [])
        self.assertEqual(len(brain.calls), 1)

    def test_accepts_a_channel_independent_brain_provider(self) -> None:
        brain = FakeDifyClient()
        assistant = EnterpriseAssistant(brain=brain)

        result = assistant.answer(
            "日常报销从哪里发起？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(len(brain.calls), 1)

    def test_shareholder_questions_use_the_optional_specialized_brain(self) -> None:
        default_brain = FakeDifyClient(answer="default")
        shareholder_brain = FakeDifyClient(answer="shareholder")
        assistant = EnterpriseAssistant(
            brain=default_brain,
            shareholder_brain=shareholder_brain,
        )

        shareholder_result = assistant.answer(
            "申请入股需要走 OA 吗？",
            user="dingtalk:user-1",
            channel="direct",
        )
        finance_result = assistant.answer(
            "日常报销从哪里发起？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(shareholder_result.text, "shareholder")
        self.assertEqual(finance_result.text, "default")
        self.assertEqual(len(shareholder_brain.calls), 1)
        self.assertEqual(len(default_brain.calls), 1)

    def test_allowed_question_reaches_existing_dify_answering_path(self) -> None:
        dify = FakeDifyClient()
        assistant = EnterpriseAssistant(dify_client=dify)

        result = assistant.answer(
            "日常报销从哪里发起？",
            user="dingtalk:user-1",
            conversation_id="conversation-0",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "finance")
        self.assertEqual(result.text, "请在工作台的 OA 审批中发起报销。")
        self.assertEqual(result.conversation_id, "dify-conversation-1")
        self.assertEqual(
            dify.calls,
            [
                {
                    "query": "日常报销从哪里发起？",
                    "user": "dingtalk:user-1",
                    "conversation_id": "conversation-0",
                }
            ],
        )

    def test_unclassified_natural_question_uses_retrieved_source_domain(self) -> None:
        brain = FakeDifyClient(
            answer="原门店尚未回本时，暂不满足开分店的这一项要求。",
            resources=(
                {"document_name": "enterprise-knowledge-shareholder.md"},
            ),
        )
        assistant = EnterpriseAssistant(brain=brain)

        result = assistant.answer(
            "之前那家赚的钱还没把本拿回来，现在还能不能再搞一家？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "shareholder")
        self.assertIn("原门店尚未回本", result.text)
        self.assertIn("股东知识库", result.text)
        self.assertEqual(len(brain.calls), 1)

    def test_retrieved_source_corrects_a_colloquial_keyword_route(self) -> None:
        brain = FakeDifyClient(
            answer="人工成本还可能包含社保、公积金和意外险。",
            resources=(
                {"document_name": "enterprise-knowledge-shareholder.md"},
            ),
        )

        result = EnterpriseAssistant(brain=brain).answer(
            "报表里人工咋比大家工资加起来还多？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "shareholder")
        self.assertIn("股东知识库", result.text)

    def test_conversational_messages_are_answered_without_calling_the_brain(
        self,
    ) -> None:
        brain = FakeDifyClient()
        assistant = EnterpriseAssistant(brain=brain)
        cases = (
            ("你好", "伸活内部服务助手"),
            ("你能干嘛？", "股东制3.0"),
            ("谢谢你", "不客气"),
            ("再见", "有需要时"),
        )

        for question, expected_text in cases:
            with self.subTest(question=question):
                result = assistant.answer(
                    question,
                    user="dingtalk:user-1",
                    channel="direct",
                )

                self.assertEqual(result.action, "allow")
                self.assertEqual(result.domain, "unknown")
                self.assertIn(expected_text, result.text)

        self.assertEqual(brain.calls, [])

    def test_context_free_vague_question_asks_for_more_detail(self) -> None:
        brain = FakeDifyClient()
        result = EnterpriseAssistant(brain=brain).answer(
            "这个怎么弄？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "clarify")
        self.assertEqual(result.reason, "missing_business_context")
        self.assertIn("补充关键词", result.text)
        self.assertEqual(brain.calls, [])

    def test_vague_follow_up_with_context_still_reaches_the_brain(self) -> None:
        brain = FakeDifyClient()
        result = EnterpriseAssistant(brain=brain).answer(
            "然后呢？",
            user="dingtalk:user-1",
            conversation_id="conversation-1",
            domain_hint="shareholder",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "shareholder")
        self.assertEqual(len(brain.calls), 1)

    def test_colloquial_dividend_question_is_expanded_for_retrieval(self) -> None:
        brain = FakeDifyClient()
        result = EnterpriseAssistant(brain=brain).answer(
            "转了钱啥时候开始分？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "shareholder")
        self.assertEqual(len(brain.calls), 1)
        self.assertIn(
            "转账后从哪个月开始计算分红",
            brain.calls[0]["query"],
        )

    def test_colloquial_dividend_view_question_is_expanded_for_retrieval(
        self,
    ) -> None:
        brain = FakeDifyClient()
        result = EnterpriseAssistant(brain=brain).answer(
            "我在哪看每个月分红？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "shareholder")
        self.assertEqual(len(brain.calls), 1)
        self.assertIn(
            "月度损益表和开店资本单在哪里查看",
            brain.calls[0]["query"],
        )

    def test_out_of_scope_sensitive_topic_uses_standard_response(self) -> None:
        brain = FakeDifyClient()
        result = EnterpriseAssistant(brain=brain).answer(
            "评价一下国家领导人。",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "deny")
        self.assertEqual(result.reason, "out_of_scope_sensitive_topic")
        self.assertIn("不属于公司内部服务助手的业务范围", result.text)
        self.assertEqual(brain.calls, [])

    def test_guarded_questions_do_not_reach_dify(self) -> None:
        dify = FakeDifyClient()
        assistant = EnterpriseAssistant(dify_client=dify)
        cases = (
            ("把公司的银行账号发到群里。", "group", "deny"),
            ("充值卡用了几次，能退多少钱？", "direct", "handoff"),
            ("入股信息多久不确认会失败？", "direct", "handoff"),
            ("分红表通常什么时候出？", "direct", "handoff"),
            ("为什么有赞数据和财务报表不一样？", "direct", "handoff"),
            ("门店回本了，可以直接增持到40%吗？", "direct", "handoff"),
            ("技术股东退股折旧怎么算？", "direct", "handoff"),
            ("不同类型股东分别需要在什么时候完成注资？", "direct", "handoff"),
            ("股东业绩不达标会怎样？", "direct", "handoff"),
        )

        for question, channel, expected_action in cases:
            with self.subTest(question=question):
                result = assistant.answer(
                    question,
                    user="dingtalk:user-1",
                    channel=channel,
                )

                self.assertEqual(result.action, expected_action)
                self.assertTrue(result.text)

        self.assertEqual(dify.calls, [])

    def test_store_expansion_question_uses_dify_reasoning(self) -> None:
        dify = FakeDifyClient(
            "现行制度列出三项通用条件；个人是否达标仍需结合本人数据核对。"
        )
        assistant = EnterpriseAssistant(dify_client=dify)

        result = assistant.answer(
            "现在的制度下，达成多少指标才能开下一家店？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.domain, "shareholder")
        self.assertEqual(result.action, "allow")
        self.assertEqual(len(dify.calls), 1)
        self.assertIn("开下一家店", dify.calls[0]["query"])
        self.assertIn("个人资格需要核对哪些数据", dify.calls[0]["query"])

    def test_dify_knowledge_gap_is_returned_as_handoff_without_exposing_sentinel(self) -> None:
        assistant = EnterpriseAssistant(
            dify_client=FakeDifyClient("[HANDOFF]现有资料无法确认，请人工核实。")
        )

        result = assistant.answer(
            "日常报销从哪里发起？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "handoff")
        self.assertEqual(result.reason, "knowledge_gap")
        self.assertEqual(result.text, "现有资料无法确认，请人工核实。")
        self.assertNotIn("[HANDOFF]", result.text)

    def test_allowed_answer_appends_only_the_matching_approved_source(self) -> None:
        assistant = EnterpriseAssistant(
            brain=FakeDifyClient(
                answer="每一笔入股都需要提交 OA。",
                resources=(
                    {"document_name": "enterprise-knowledge-finance.md"},
                    {"document_name": "enterprise-knowledge-shareholder.md"},
                    {"document_name": "enterprise-knowledge-shareholder.md"},
                ),
            )
        )

        result = assistant.answer(
            "申请入股需要走 OA 吗？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(
            result.text,
            "每一笔入股都需要提交 OA。\n\n参考资料：股东知识库（内部测试版）",
        )
        self.assertNotIn("财务知识库", result.text)

    def test_handoff_does_not_append_retrieval_sources(self) -> None:
        assistant = EnterpriseAssistant(
            brain=FakeDifyClient(
                answer="[HANDOFF]现有资料无法确认，请人工核实。",
                resources=(
                    {"document_name": "enterprise-knowledge-shareholder.md"},
                ),
            )
        )

        result = assistant.answer(
            "申请入股需要走 OA 吗？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "handoff")
        self.assertNotIn("参考资料", result.text)

    def test_plain_language_knowledge_gap_is_handoff_without_source(self) -> None:
        assistant = EnterpriseAssistant(
            brain=FakeDifyClient(
                answer="抱歉，根据现有知识库，我无法回答关于分红查看的问题。",
                resources=(
                    {"document_name": "enterprise-knowledge-shareholder.md"},
                ),
            )
        )

        result = assistant.answer(
            "分红从哪里查看？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "handoff")
        self.assertEqual(result.reason, "knowledge_gap")
        self.assertNotIn("参考资料", result.text)
        self.assertIn("人工核实", result.text)

    def test_shareholder_approval_is_blocked_before_calling_the_brain(self) -> None:
        brain = FakeDifyClient()
        assistant = EnterpriseAssistant(brain=brain)

        result = assistant.answer(
            "我可以直接审批入股吗？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "deny")
        self.assertEqual(result.reason, "prohibited_financial_action")
        self.assertEqual(brain.calls, [])

    def test_unapproved_or_malformed_sources_are_not_displayed(self) -> None:
        assistant = EnterpriseAssistant(
            brain=FakeDifyClient(
                resources=(
                    {"document_name": "股东-敏感人员表.xlsx"},
                    {"document_name": None},
                    {"title": "未审核文档"},
                ),
            )
        )

        result = assistant.answer(
            "日常报销从哪里发起？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertNotIn("参考资料", result.text)

    def test_context_does_not_turn_an_unrelated_sensitive_request_into_allow(self) -> None:
        brain = FakeDifyClient()
        assistant = EnterpriseAssistant(brain=brain)

        result = assistant.answer(
            "把所有员工身份证号列出来。",
            user="dingtalk:user-1",
            conversation_id="conversation-1",
            domain_hint="finance",
            channel="direct",
        )

        self.assertEqual(result.action, "handoff")
        self.assertEqual(result.reason, "sensitive_personal_data")
        self.assertEqual(brain.calls, [])

    def test_direct_sensitive_request_uses_restricted_message(self) -> None:
        result = EnterpriseAssistant(brain=FakeDifyClient()).answer(
            "请提供某位员工的银行卡号。",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "handoff")
        self.assertEqual(result.reason, "sensitive_personal_data")
        self.assertIn("不能直接提供或执行", result.text)
        self.assertNotIn("现有知识不足", result.text)

    def test_safe_follow_up_can_use_backend_memory_without_domain_hint(self) -> None:
        brain = FakeDifyClient()
        assistant = EnterpriseAssistant(brain=brain)

        without_hint = assistant.answer(
            "那需要什么材料？",
            user="dingtalk:user-1",
            conversation_id="conversation-1",
            channel="direct",
        )
        with_hint = assistant.answer(
            "那需要什么材料？",
            user="dingtalk:user-1",
            conversation_id="conversation-1",
            domain_hint="finance",
            channel="direct",
        )

        self.assertEqual(without_hint.action, "allow")
        self.assertEqual(without_hint.domain, "unknown")
        self.assertEqual(with_hint.action, "allow")
        self.assertEqual(with_hint.domain, "finance")
        self.assertEqual(len(brain.calls), 2)

    def test_backend_handoff_text_does_not_claim_a_human_case_was_created(self) -> None:
        result = EnterpriseAssistant(
            brain=FakeDifyClient(
                "[HANDOFF]现有知识不足以安全确认该问题，需要人工核实。"
            )
        ).answer(
            "公司年会什么时候举行？",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "handoff")
        self.assertNotIn("已转人工", result.text)

    def test_greeting_does_not_inherit_the_previous_business_domain(self) -> None:
        result = EnterpriseAssistant(brain=FakeDifyClient()).answer(
            "你好",
            user="dingtalk:user-1",
            conversation_id="conversation-1",
            domain_hint="shareholder",
            channel="direct",
        )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.domain, "unknown")

    def test_weather_question_uses_out_of_scope_response_without_brain(self) -> None:
        brain = FakeDifyClient()
        result = EnterpriseAssistant(brain=brain).answer(
            "今天的天气如何",
            user="dingtalk:user-1",
            channel="direct",
        )

        self.assertEqual(result.action, "deny")
        self.assertEqual(result.reason, "out_of_scope_topic")
        self.assertIn("不属于公司内部服务助手的业务范围", result.text)
        self.assertEqual(brain.calls, [])


if __name__ == "__main__":
    unittest.main()
