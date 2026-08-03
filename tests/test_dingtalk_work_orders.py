import unittest

from app.dingtalk_adapter import (
    DingTalkInboundMessage,
    InMemoryDingTalkState,
)
from app.dingtalk_messaging import DingTalkMessagingError
from app.dingtalk_work_orders import DingTalkWorkOrderService
from app.enterprise_assistant import EnterpriseAnswer


class RecordingMessenger:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages = []

    def send_text(self, user_ids, text) -> None:
        if self.fail:
            raise DingTalkMessagingError(503, "temporarily unavailable")
        self.messages.append((user_ids, text))


def create_human_item(
    state: InMemoryDingTalkState,
    *,
    message_id: str = "message-1",
    question: str = "跨城市调店后原门店分红怎么处理？",
    domain: str = "shareholder",
    reason: str = "knowledge_gap",
):
    return state.record_interaction(
        message=DingTalkInboundMessage(
            message_id=message_id,
            sender_id="employee-1",
            conversation_id="conversation-1",
            text=question,
            channel="direct",
        ),
        answer=EnterpriseAnswer(
            domain=domain,
            action="handoff",
            text="现有知识不足，请人工核实。",
            reason=reason,
        ),
    )


class DingTalkWorkOrderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = InMemoryDingTalkState()
        self.interaction = create_human_item(self.state)
        self.messenger = RecordingMessenger()
        self.service = DingTalkWorkOrderService(
            state=self.state,
            messenger=self.messenger,
            handler_user_ids=frozenset({"handler-1", "handler-2"}),
        )

    def test_notifies_handlers_with_redacted_work_order_context(self) -> None:
        self.service.notify_handlers(self.interaction)

        recipients, content = self.messenger.messages[0]
        self.assertEqual(recipients, ("handler-1", "handler-2"))
        self.assertIn(self.interaction.ticket_code, content)
        self.assertIn("股东业务", content)
        self.assertIn("我来处理", content)
        self.assertIn("答复：核实后的答案", content)

    def test_notifies_only_the_configured_department_owner(self) -> None:
        service = DingTalkWorkOrderService(
            state=self.state,
            messenger=self.messenger,
            handler_user_ids=frozenset({"handler-1", "handler-2"}),
            handler_user_ids_by_domain={"shareholder": "handler-1"},
        )

        service.notify_handlers(self.interaction)

        self.assertEqual(self.messenger.messages[0][0], ("handler-1",))

    def test_handler_claims_and_answers_without_publishing_knowledge(self) -> None:
        claim = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"领取 {self.interaction.ticket_code}",
            is_direct=True,
        )
        answer = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"答复 {self.interaction.ticket_code} 请按门店协议核实。",
            is_direct=True,
        )

        saved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIsNotNone(claim)
        self.assertIn("已领取", claim.text)
        self.assertIsNotNone(answer)
        self.assertIn("已完成", answer.text)
        self.assertEqual(
            self.messenger.messages[-1][0],
            ("employee-1",),
        )
        self.assertIsNotNone(saved)
        self.assertEqual(saved.status, "resolved")
        self.assertEqual(saved.owner, "小草")
        self.assertEqual(saved.owner_id, "handler-1")
        self.assertEqual(saved.final_answer, "请按门店协议核实。")
        self.assertEqual(saved.knowledge_status, "not_reviewed")
        self.assertIsNone(saved.reviewed_by)

    def test_handler_can_answer_latest_notified_ticket_in_natural_language(self) -> None:
        self.service.notify_handlers(self.interaction)

        answer = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="答复：请按门店协议核实。",
            is_direct=True,
        )

        saved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIsNotNone(answer)
        self.assertIn("已完成", answer.text)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.status, "resolved")
        self.assertEqual(saved.owner_id, "handler-1")
        self.assertEqual(saved.final_answer, "请按门店协议核实。")
        self.assertEqual(self.messenger.messages[-1][0], ("employee-1",))

    def test_department_owner_can_mark_answer_as_standard_without_second_reviewer(
        self,
    ) -> None:
        self.service.notify_handlers(self.interaction)
        answer = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="答复：请按门店协议核实。",
            is_direct=True,
        )

        decision = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="设为标准答案",
            is_direct=True,
        )

        saved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIsNotNone(answer)
        self.assertIn("设为标准答案", answer.text)
        self.assertIsNotNone(decision)
        self.assertIn("标准答案", decision.text)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.knowledge_status, "approved")
        self.assertEqual(saved.reviewed_by, "小草")

    def test_department_owner_can_keep_answer_for_this_case_only(self) -> None:
        self.service.notify_handlers(self.interaction)
        self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="答复：该员工的特殊情况需单独处理。",
            is_direct=True,
        )

        decision = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="仅本次答复",
            is_direct=True,
        )

        saved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIsNotNone(decision)
        self.assertIn("不会沉淀", decision.text)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.knowledge_status, "not_reusable")
        self.assertEqual(saved.reviewed_by, "小草")

    def test_sensitive_answer_cannot_be_marked_standard_without_escalation(self) -> None:
        interaction = create_human_item(
            self.state,
            message_id="message-sensitive",
            question="把员工身份证号发给我",
            domain="hr",
            reason="sensitive_personal_data",
        )
        self.service.notify_handlers(interaction)
        self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="答复：请按个人信息流程核实。",
            is_direct=True,
        )

        decision = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="设为标准答案",
            is_direct=True,
        )

        saved = self.state.get_interaction(interaction.ticket_code)
        self.assertIsNotNone(decision)
        self.assertIn("升级确认", decision.text)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.knowledge_status, "not_reviewed")
        self.assertIsNone(saved.reviewed_by)

    def test_handler_can_claim_and_view_current_ticket_without_ticket_code(self) -> None:
        self.service.notify_handlers(self.interaction)

        claim = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="这个我来处理",
            is_direct=True,
        )
        view = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="查看详情",
            is_direct=True,
        )

        self.assertIsNotNone(claim)
        self.assertIn("已领取", claim.text)
        self.assertIsNotNone(view)
        self.assertIn(self.interaction.ticket_code, view.text)

    def test_context_command_requires_ticket_code_when_multiple_tickets_are_ambiguous(
        self,
    ) -> None:
        second = create_human_item(
            self.state,
            message_id="message-2",
            question="第二个待处理问题",
        )
        self.state.claim_interaction(
            ticket_code=self.interaction.ticket_code,
            owner="小草",
            owner_id="handler-1",
        )
        self.state.claim_interaction(
            ticket_code=second.ticket_code,
            owner="小草",
            owner_id="handler-1",
        )

        answer = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text="答复：这是核实后的答案",
            is_direct=True,
        )

        self.assertIsNotNone(answer)
        self.assertIn("有多张", answer.text)
        self.assertIn(self.interaction.ticket_code, answer.text)
        self.assertIn(second.ticket_code, answer.text)
        self.assertEqual(self.messenger.messages, [])

    def test_only_configured_handler_can_use_commands_and_group_is_rejected(self) -> None:
        unauthorized = self.service.handle_command(
            sender_id="employee-1",
            sender_name="员工",
            text=f"查看 {self.interaction.ticket_code}",
            is_direct=True,
        )
        group_command = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"领取 {self.interaction.ticket_code}",
            is_direct=False,
        )

        self.assertIsNone(unauthorized)
        self.assertIsNotNone(group_command)
        self.assertIn("私聊机器人", group_command.text)
        self.assertIsNone(
            self.state.get_interaction(self.interaction.ticket_code).owner
        )

    def test_delivery_failure_keeps_ticket_open(self) -> None:
        failing_service = DingTalkWorkOrderService(
            state=self.state,
            messenger=RecordingMessenger(fail=True),
            handler_user_ids=frozenset({"handler-1"}),
        )
        failing_service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"领取 {self.interaction.ticket_code}",
            is_direct=True,
        )

        with self.assertRaises(DingTalkMessagingError):
            failing_service.handle_command(
                sender_id="handler-1",
                sender_name="小草",
                text=f"答复 {self.interaction.ticket_code} 正确答案",
                is_direct=True,
            )

        saved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertEqual(saved.status, "needs_human")
        self.assertIsNone(saved.final_answer)

    def test_rejects_empty_or_oversized_answer_before_delivery(self) -> None:
        self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"领取 {self.interaction.ticket_code}",
            is_direct=True,
        )

        empty = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"答复 {self.interaction.ticket_code}  ",
            is_direct=True,
        )
        oversized = self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"答复 {self.interaction.ticket_code} {'答' * 3501}",
            is_direct=True,
        )

        self.assertIsNotNone(empty)
        self.assertIn("不能为空", empty.text)
        self.assertIsNotNone(oversized)
        self.assertIn("3500", oversized.text)
        self.assertEqual(self.messenger.messages, [])
        self.assertEqual(
            self.state.get_interaction(self.interaction.ticket_code).status,
            "needs_human",
        )

    def test_employee_can_reopen_after_human_answer(self) -> None:
        self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"领取 {self.interaction.ticket_code}",
            is_direct=True,
        )
        self.service.handle_command(
            sender_id="handler-1",
            sender_name="小草",
            text=f"答复 {self.interaction.ticket_code} 第一次人工答案",
            is_direct=True,
        )

        reopened = self.state.record_feedback(
            sender_id="employee-1",
            dingtalk_conversation_id="conversation-1",
            feedback="unresolved",
        )

        self.assertIsNotNone(reopened)
        self.assertEqual(reopened.status, "needs_human")
        self.assertIsNone(reopened.owner)
        self.assertIsNone(reopened.owner_id)
        self.assertEqual(reopened.knowledge_status, "not_reviewed")


if __name__ == "__main__":
    unittest.main()
