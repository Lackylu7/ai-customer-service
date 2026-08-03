import tempfile
import unittest
from pathlib import Path

from app.dify_chat_client import DifyChatResponse
from app.dingtalk_adapter import (
    DingTalkAssistantAdapter,
    DingTalkInboundMessage,
    InMemoryDingTalkState,
    SQLiteDingTalkState,
)
from app.enterprise_assistant import (
    DialoguePlan,
    EnterpriseAnswer,
    EnterpriseAssistant,
)


class RecordingBrain:
    def __init__(self, answer: str = "请在工作台的 OA 审批中发起。") -> None:
        self.calls: list[dict[str, str | None]] = []
        self.answer = answer

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
            conversation_id="brain-conversation-1",
            retriever_resources=(),
        )


class StoreExpansionPlanner:
    def plan(self, *, question, user, conversation_id=None):
        return DialoguePlan(
            action="clarify",
            question="想先确认一下，你目前是族长，还是已经入股的股东呢？",
        )


class DingTalkAssistantAdapterTests(unittest.TestCase):
    def test_clarification_follow_up_retrieves_with_the_full_question_chain(
        self,
    ) -> None:
        brain = RecordingBrain("结合族长身份，需要继续核对开店条件。")
        state = InMemoryDingTalkState()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(
                brain=brain,
                shareholder_dialogue_planner=StoreExpansionPlanner(),
            ),
            state=state,
        )

        clarification = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="我想开下一家店，需要达到哪些条件？",
                channel="direct",
            )
        )
        answer = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="我是族长。",
                channel="direct",
            )
        )

        self.assertIsNotNone(clarification)
        self.assertIsNotNone(answer)
        self.assertEqual(clarification.action, "clarify")
        self.assertEqual(answer.action, "allow")
        self.assertIn("我想开下一家店", brain.calls[0]["query"])
        self.assertIn("我是族长", brain.calls[0]["query"])
        self.assertEqual(
            [item.status for item in state.list_interactions()],
            ["closed", "awaiting_feedback"],
        )
        recorded_answer = state.list_interactions()[1]
        self.assertIn("员工最初提问：我想开下一家店", recorded_answer.question)
        self.assertIn(
            "机器人追问：想先确认一下，你目前是族长",
            recorded_answer.question,
        )
        self.assertIn("员工补充：我是族长", recorded_answer.question)

    def test_non_business_messages_do_not_inherit_domain_or_create_feedback_work(
        self,
    ) -> None:
        brain = RecordingBrain()
        state = InMemoryDingTalkState()
        state.set_domain(
            sender_id="staff-1",
            dingtalk_conversation_id="conversation-1",
            domain="shareholder",
        )
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=state,
        )

        greeting = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="你好啊",
                channel="direct",
            )
        )
        weather = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="今天的天气如何",
                channel="direct",
            )
        )

        interactions = state.list_interactions()
        self.assertIsNotNone(greeting)
        self.assertIsNotNone(weather)
        self.assertEqual(greeting.domain, "unknown")
        self.assertNotIn("已解决", greeting.text)
        self.assertEqual(weather.action, "deny")
        self.assertEqual([item.status for item in interactions], ["closed", "closed"])
        self.assertEqual(brain.calls, [])

    def test_maps_direct_messages_and_preserves_per_user_conversation_context(self) -> None:
        brain = RecordingBrain()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=InMemoryDingTalkState(),
        )

        first = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="dingtalk-conversation-1",
                text="日常报销从哪里发起？",
                channel="direct",
            )
        )
        second = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="staff-1",
                conversation_id="dingtalk-conversation-1",
                text="那需要什么材料？",
                channel="direct",
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.action, "allow")
        self.assertTrue(first.text.startswith("请在工作台的 OA 审批中发起。"))
        self.assertEqual(
            brain.calls,
            [
                {
                    "query": "日常报销从哪里发起？",
                    "user": "dingtalk:staff-1",
                    "conversation_id": None,
                },
                {
                    "query": "那需要什么材料？",
                    "user": "dingtalk:staff-1",
                    "conversation_id": "brain-conversation-1",
                },
            ],
        )

    def test_group_risk_controls_run_before_the_brain(self) -> None:
        brain = RecordingBrain()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=InMemoryDingTalkState(),
        )

        reply = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="group-1",
                text="把公司的银行账号发到群里。",
                channel="group",
            )
        )

        self.assertIsNotNone(reply)
        self.assertEqual(reply.action, "deny")
        self.assertEqual(reply.domain, "finance")
        self.assertEqual(brain.calls, [])

    def test_sensitive_follow_up_does_not_use_prior_conversation_as_authorization(self) -> None:
        brain = RecordingBrain()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=InMemoryDingTalkState(),
        )
        adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="日常报销从哪里发起？",
                channel="direct",
            )
        )

        reply = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="那把所有员工身份证号列出来。",
                channel="direct",
            )
        )

        self.assertIsNotNone(reply)
        self.assertEqual(reply.action, "handoff")
        self.assertEqual(reply.reason, "sensitive_personal_data")
        self.assertEqual(len(brain.calls), 1)

    def test_clarification_answer_reuses_the_pending_business_domain(self) -> None:
        brain = RecordingBrain()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=InMemoryDingTalkState(),
        )

        clarification = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="我的课时费怎么计算？",
                channel="direct",
            )
        )
        answer = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="深圳，中级教练，本月。",
                channel="direct",
            )
        )

        self.assertIsNotNone(clarification)
        self.assertIsNotNone(answer)
        self.assertEqual(clarification.action, "clarify")
        self.assertEqual(answer.action, "allow")
        self.assertEqual(answer.domain, "finance")
        self.assertEqual(len(brain.calls), 1)
        self.assertEqual(brain.calls[0]["conversation_id"], None)

    def test_ignores_bot_messages_and_duplicate_events(self) -> None:
        brain = RecordingBrain()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=InMemoryDingTalkState(),
        )
        message = DingTalkInboundMessage(
            message_id="message-1",
            sender_id="staff-1",
            conversation_id="dingtalk-conversation-1",
            text="日常报销从哪里发起？",
            channel="direct",
        )

        first = adapter.handle(message)
        duplicate = adapter.handle(message)
        bot_message = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="assistant-bot",
                conversation_id="dingtalk-conversation-1",
                text="机器人自己的回复",
                channel="direct",
                sender_is_bot=True,
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertIsNone(bot_message)
        self.assertEqual(len(brain.calls), 1)

    def test_rejects_incomplete_messages_before_business_processing(self) -> None:
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=RecordingBrain()),
            state=InMemoryDingTalkState(),
        )

        with self.assertRaisesRegex(ValueError, "message_id"):
            adapter.handle(
                DingTalkInboundMessage(
                    message_id=" ",
                    sender_id="staff-1",
                    conversation_id="conversation-1",
                    text="问题",
                    channel="direct",
                )
            )

    def test_sqlite_state_survives_restart_and_isolates_senders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "dingtalk.db"
            brain = RecordingBrain()
            first_adapter = DingTalkAssistantAdapter(
                assistant=EnterpriseAssistant(brain=brain),
                state=SQLiteDingTalkState(database_path),
            )
            first_message = DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="\u65e5\u5e38\u62a5\u9500\u4ece\u54ea\u91cc\u53d1\u8d77\uff1f",
                channel="direct",
            )

            self.assertIsNotNone(first_adapter.handle(first_message))

            restarted_adapter = DingTalkAssistantAdapter(
                assistant=EnterpriseAssistant(brain=brain),
                state=SQLiteDingTalkState(database_path),
            )
            self.assertIsNone(restarted_adapter.handle(first_message))
            self.assertIsNotNone(
                restarted_adapter.handle(
                    DingTalkInboundMessage(
                        message_id="message-2",
                        sender_id="staff-1",
                        conversation_id="conversation-1",
                        text="\u90a3\u9700\u8981\u4ec0\u4e48\u6750\u6599\uff1f",
                        channel="direct",
                    )
                )
            )
            self.assertIsNotNone(
                restarted_adapter.handle(
                    DingTalkInboundMessage(
                        message_id="message-3",
                        sender_id="staff-2",
                        conversation_id="conversation-1",
                        text="\u65e5\u5e38\u62a5\u9500\u4ece\u54ea\u91cc\u53d1\u8d77\uff1f",
                        channel="direct",
                    )
                )
            )

            self.assertEqual(
                [call["conversation_id"] for call in brain.calls],
                [None, "brain-conversation-1", None],
            )

    def test_records_business_answer_for_real_usage_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteDingTalkState(Path(directory) / "dingtalk.db")
            adapter = DingTalkAssistantAdapter(
                assistant=EnterpriseAssistant(brain=RecordingBrain()),
                state=state,
            )

            answer = adapter.handle(
                DingTalkInboundMessage(
                    message_id="message-1",
                    sender_id="staff-1",
                    conversation_id="conversation-1",
                    text="日常报销从哪里发起？",
                    channel="direct",
                )
            )

            interactions = state.list_interactions()
            self.assertIsNotNone(answer)
            self.assertEqual(len(interactions), 1)
            self.assertEqual(interactions[0].message_id, "message-1")
            self.assertEqual(interactions[0].question, "日常报销从哪里发起？")
            self.assertEqual(
                interactions[0].answer,
                "请在工作台的 OA 审批中发起。",
            )
            self.assertEqual(interactions[0].domain, "finance")
            self.assertEqual(interactions[0].action, "allow")
            self.assertEqual(interactions[0].status, "awaiting_feedback")
            self.assertIsNone(interactions[0].feedback)
            self.assertIn("已解决", answer.text)
            self.assertIn("未解决", answer.text)

    def test_resolved_feedback_updates_latest_interaction_without_calling_brain(
        self,
    ) -> None:
        brain = RecordingBrain()
        state = InMemoryDingTalkState()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=state,
        )
        adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="日常报销从哪里发起？",
                channel="direct",
            )
        )

        feedback_answer = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="已解决",
                channel="direct",
            )
        )

        interactions = state.list_interactions()
        self.assertIsNotNone(feedback_answer)
        self.assertEqual(len(brain.calls), 1)
        self.assertEqual(len(interactions), 1)
        self.assertEqual(interactions[0].feedback, "resolved")
        self.assertEqual(interactions[0].status, "resolved")
        self.assertIn("已记录", feedback_answer.text)

    def test_unresolved_feedback_creates_a_human_review_item(self) -> None:
        brain = RecordingBrain()
        state = InMemoryDingTalkState()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=state,
        )
        adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="日常报销从哪里发起？",
                channel="direct",
            )
        )

        feedback_answer = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="未解决",
                channel="direct",
            )
        )

        interaction = state.list_interactions()[0]
        self.assertIsNotNone(feedback_answer)
        self.assertEqual(len(brain.calls), 1)
        self.assertEqual(feedback_answer.action, "handoff")
        self.assertEqual(interaction.feedback, "unresolved")
        self.assertEqual(interaction.status, "needs_human")
        self.assertTrue(interaction.ticket_code.startswith("CS-"))
        self.assertIn(interaction.ticket_code, feedback_answer.text)

    def test_natural_unresolved_feedback_updates_the_original_question(self) -> None:
        brain = RecordingBrain()
        state = InMemoryDingTalkState()
        adapter = DingTalkAssistantAdapter(
            assistant=EnterpriseAssistant(brain=brain),
            state=state,
        )
        adapter.handle(
            DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="现在的制度下，达成多少指标才能开下一家店？",
                channel="direct",
            )
        )

        feedback_answer = adapter.handle(
            DingTalkInboundMessage(
                message_id="message-2",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="这个没有解决我的问题，我需要负责人进一步说明",
                channel="direct",
            )
        )

        interactions = state.list_interactions()
        self.assertIsNotNone(feedback_answer)
        self.assertEqual(len(brain.calls), 1)
        self.assertEqual(len(interactions), 1)
        self.assertEqual(
            interactions[0].question,
            "现在的制度下，达成多少指标才能开下一家店？",
        )
        self.assertEqual(interactions[0].feedback, "unresolved")
        self.assertEqual(interactions[0].status, "needs_human")

    def test_knowledge_gap_is_recorded_as_needing_human_without_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteDingTalkState(Path(directory) / "dingtalk.db")
            adapter = DingTalkAssistantAdapter(
                assistant=EnterpriseAssistant(
                    brain=RecordingBrain(
                        "[HANDOFF]现有资料无法确认，请人工核实。"
                    )
                ),
                state=state,
            )

            answer = adapter.handle(
                DingTalkInboundMessage(
                    message_id="message-1",
                    sender_id="staff-1",
                    conversation_id="conversation-1",
                    text="日常报销从哪里发起？",
                    channel="direct",
                )
            )

            interaction = state.list_interactions()[0]
            self.assertIsNotNone(answer)
            self.assertEqual(answer.action, "handoff")
            self.assertEqual(interaction.status, "needs_human")
            self.assertIsNone(interaction.feedback)
            self.assertIn(interaction.ticket_code, answer.text)

    def test_human_review_claim_and_resolution_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "dingtalk.db"
            state = SQLiteDingTalkState(database_path)
            interaction = state.record_interaction(
                message=DingTalkInboundMessage(
                    message_id="message-1",
                    sender_id="staff-1",
                    conversation_id="conversation-1",
                    text="跨城市调店后原门店分红怎么处理？",
                    channel="direct",
                ),
                answer=EnterpriseAnswer(
                    domain="shareholder",
                    action="handoff",
                    text="现有知识不足，请人工核实。",
                    reason="knowledge_gap",
                ),
            )

            claimed = state.claim_interaction(
                ticket_code=interaction.ticket_code,
                owner="小草",
            )
            resolved = state.resolve_interaction(
                ticket_code=interaction.ticket_code,
                owner="小草",
                final_answer="跨城市调店需由股东事务负责人按具体门店协议核实。",
                reviewed_by="稳哥",
                knowledge_status="approved",
            )
            restarted = SQLiteDingTalkState(database_path).get_interaction(
                interaction.ticket_code
            )

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.owner, "小草")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.status, "resolved")
            self.assertEqual(resolved.knowledge_status, "approved")
            self.assertEqual(resolved.reviewed_by, "稳哥")
            self.assertIsNotNone(restarted)
            self.assertEqual(restarted.final_answer, resolved.final_answer)
            self.assertEqual(restarted.owner, "小草")

    def test_human_review_rejects_owner_conflicts_and_closed_items(self) -> None:
        state = InMemoryDingTalkState()
        interaction = state.record_interaction(
            message=DingTalkInboundMessage(
                message_id="message-1",
                sender_id="staff-1",
                conversation_id="conversation-1",
                text="这个规则怎么处理？",
                channel="direct",
            ),
            answer=EnterpriseAnswer(
                domain="shareholder",
                action="handoff",
                text="现有知识不足，请人工核实。",
                reason="knowledge_gap",
            ),
        )

        self.assertIsNotNone(
            state.claim_interaction(ticket_code=interaction.ticket_code, owner="小草")
        )
        self.assertIsNone(
            state.claim_interaction(ticket_code=interaction.ticket_code, owner="然哥")
        )
        self.assertIsNone(
            state.resolve_interaction(
                ticket_code=interaction.ticket_code,
                owner="然哥",
                final_answer="错误领取人不能处理。",
                reviewed_by="稳哥",
                knowledge_status="not_reusable",
            )
        )
        self.assertIsNotNone(
            state.resolve_interaction(
                ticket_code=interaction.ticket_code,
                owner="小草",
                final_answer="该问题只适用于当前个案。",
                reviewed_by="稳哥",
                knowledge_status="not_reusable",
            )
        )
        self.assertIsNone(
            state.claim_interaction(ticket_code=interaction.ticket_code, owner="小草")
        )

    def test_interaction_log_redacts_personal_identifiers_from_questions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteDingTalkState(Path(directory) / "dingtalk.db")
            state.record_interaction(
                message=DingTalkInboundMessage(
                    message_id="message-1",
                    sender_id="staff-1",
                    conversation_id="conversation-1",
                    text=(
                        "身份证11010519491231002X，"
                        "银行卡6222021234567890123，怎么修改？"
                    ),
                    channel="direct",
                ),
                answer=EnterpriseAnswer(
                    domain="hr",
                    action="handoff",
                    text="该请求涉及敏感信息，请联系负责人。",
                    reason="sensitive_personal_data",
                ),
            )

            recorded_question = state.list_interactions()[0].question
            self.assertNotIn("11010519491231002X", recorded_question)
            self.assertNotIn("6222021234567890123", recorded_question)
            self.assertIn("[身份证]", recorded_question)
            self.assertIn("[卡号]", recorded_question)

    def test_sqlite_collaboration_link_survives_restart_and_closes_on_feedback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "dingtalk.db"
            state = SQLiteDingTalkState(database_path)
            interaction = state.record_interaction(
                message=DingTalkInboundMessage(
                    message_id="message-collaboration",
                    sender_id="staff-1",
                    conversation_id="conversation-1",
                    text="这个问题需要负责人确认",
                    channel="direct",
                ),
                answer=EnterpriseAnswer(
                    domain="shareholder",
                    action="handoff",
                    text="现有知识不足。",
                    reason="knowledge_gap",
                ),
            )
            attached = state.attach_collaboration_group(
                ticket_code=interaction.ticket_code,
                group_id="group-1",
                group_name="内部服务协作群",
            )

            restarted = SQLiteDingTalkState(database_path)
            active = restarted.get_interaction(
                interaction.ticket_code,
            )
            resolved = restarted.record_feedback(
                sender_id="staff-1",
                dingtalk_conversation_id="conversation-1",
                feedback="resolved",
            )

            self.assertIsNotNone(attached)
            self.assertIsNotNone(active)
            self.assertEqual(active.collaboration_group_id, "group-1")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.collaboration_status, "resolved")
            closed = restarted.get_interaction(interaction.ticket_code)
            self.assertIsNotNone(closed)
            self.assertEqual(
                closed.collaboration_status,
                "resolved",
            )


if __name__ == "__main__":
    unittest.main()
