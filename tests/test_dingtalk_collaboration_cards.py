import asyncio
import json
import unittest
from types import SimpleNamespace

from dingtalk_stream import AckMessage

from app.dingtalk_adapter import DingTalkInboundMessage, InMemoryDingTalkState
from app.dingtalk_collaboration_cards import (
    DingTalkCollaborationCardHandler,
    DingTalkCollaborationCardService,
)
from app.enterprise_assistant import EnterpriseAnswer


class RecordingCardMessenger:
    def __init__(self):
        self.sent = []
        self.updated = []

    def send_group_card(self, **kwargs):
        self.sent.append(kwargs)

    def update_group_card(self, **kwargs):
        self.updated.append(kwargs)


class RecordingClosure:
    def __init__(self):
        self.commands = []

    def can_manage(self, *, interaction, sender_id):
        del interaction
        return sender_id in {"owner-1", "operator-1"}

    def handle_group_command(self, **kwargs):
        self.commands.append(kwargs)
        if kwargs["text"] == "完成并沉淀":
            return "问题 CS-20260729-000001 已结案并已沉淀到正式知识库。"
        if kwargs["text"] == "总结并结案":
            return (
                "【结案草稿】\n最终答复：已核实\n"
                "问题成因：规则不清楚\n候选知识：开店条件说明"
            )
        return "问题 CS-20260729-000001 已结案。"


def create_interaction(state):
    interaction = state.record_interaction(
        message=DingTalkInboundMessage(
            message_id="message-1",
            sender_id="employee-1",
            conversation_id="direct-1",
            text="我要多少业绩才能开店？",
            channel="direct",
        ),
        answer=EnterpriseAnswer(
            domain="shareholder",
            action="handoff",
            text="需要负责人核实。",
            reason="knowledge_gap",
        ),
    )
    return state.attach_collaboration_group(
        ticket_code=interaction.ticket_code,
        group_id="group-1",
        group_name="股东业务协作群",
    )


class DingTalkCollaborationCardServiceTests(unittest.TestCase):
    def setUp(self):
        self.state = InMemoryDingTalkState()
        self.interaction = create_interaction(self.state)
        self.messenger = RecordingCardMessenger()
        self.closure = RecordingClosure()
        self.service = DingTalkCollaborationCardService(
            state=self.state,
            closure=self.closure,
            messenger=self.messenger,
            operator_user_ids=("operator-1",),
        )

    def test_sends_open_card_with_complete_and_continue_buttons(self):
        card_biz_id = self.service.send_open_card(
            interaction=self.interaction,
            group_id="group-1",
            status="卡片功能冒烟测试",
        )

        self.assertEqual(
            card_biz_id,
            f"internal-service-closure-v2-{self.interaction.ticket_code}",
        )
        sent = self.messenger.sent[0]
        serialized = json.dumps(sent["card_data"], ensure_ascii=False)
        self.assertEqual(sent["group_id"], "group-1")
        self.assertIn("完成并沉淀", serialized)
        self.assertIn("继续沟通", serialized)
        self.assertIn("卡片功能冒烟测试", serialized)
        self.assertIn(self.interaction.question, serialized)

    def test_operator_can_generate_and_confirm_closure(self):
        card_biz_id = self.service.send_open_card(
            interaction=self.interaction,
            group_id="group-1",
        )

        draft = self.service.handle_action(
            card_biz_id=card_biz_id,
            sender_id="operator-1",
            action="generate_closure",
        )
        resolved = self.service.handle_action(
            card_biz_id=card_biz_id,
            sender_id="operator-1",
            action="confirm_closure",
        )

        self.assertIn("结案草稿", draft)
        self.assertIn("已结案", resolved)
        self.assertEqual(
            [item["text"] for item in self.closure.commands],
            ["总结并结案", "确认结案"],
        )
        first_update = json.dumps(
            self.messenger.updated[0]["card_data"], ensure_ascii=False
        )
        second_update = json.dumps(
            self.messenger.updated[1]["card_data"], ensure_ascii=False
        )
        self.assertIn("确认并结案", first_update)
        self.assertIn("已结案", second_update)
        self.assertNotIn("confirm_closure", second_update)

    def test_operator_can_complete_and_publish_in_one_action(self):
        card_biz_id = self.service.send_open_card(
            interaction=self.interaction,
            group_id="group-1",
        )

        result = self.service.handle_action(
            card_biz_id=card_biz_id,
            sender_id="operator-1",
            action="complete_and_publish",
        )

        self.assertIn("已沉淀到正式知识库", result)
        self.assertEqual(
            [item["text"] for item in self.closure.commands],
            ["完成并沉淀"],
        )
        updated = json.dumps(
            self.messenger.updated[0]["card_data"], ensure_ascii=False
        )
        self.assertIn("已结案", updated)
        self.assertIn("正式知识库", updated)

    def test_employee_cannot_operate_closure_card(self):
        result = self.service.handle_action(
            card_biz_id=(
                f"internal-service-closure-v2-{self.interaction.ticket_code}"
            ),
            sender_id="employee-1",
            action="generate_closure",
        )

        self.assertIn("负责人或项目管理员", result)
        self.assertEqual(self.closure.commands, [])
        self.assertEqual(self.messenger.updated, [])

    def test_continue_dialogue_does_not_generate_draft(self):
        result = self.service.handle_action(
            card_biz_id=(
                f"internal-service-closure-v2-{self.interaction.ticket_code}"
            ),
            sender_id="owner-1",
            action="continue_dialogue",
        )

        self.assertIn("继续沟通", result)
        self.assertEqual(self.closure.commands, [])
        updated = json.dumps(
            self.messenger.updated[0]["card_data"], ensure_ascii=False
        )
        self.assertIn("继续沟通中", updated)


class DingTalkCollaborationCardHandlerTests(unittest.TestCase):
    def test_parses_nested_request_action_callback(self):
        state = InMemoryDingTalkState()
        interaction = create_interaction(state)
        messenger = RecordingCardMessenger()
        closure = RecordingClosure()
        service = DingTalkCollaborationCardService(
            state=state,
            closure=closure,
            messenger=messenger,
            operator_user_ids=("operator-1",),
        )
        handler = DingTalkCollaborationCardHandler(service=service)
        callback = SimpleNamespace(
            data={
                "outTrackId": (
                    f"internal-service-closure-v2-{interaction.ticket_code}"
                ),
                "userId": "operator-1",
                "content": json.dumps(
                    {"action": {"componentId": "generate_closure"}}
                ),
            }
        )

        status, response = asyncio.run(handler.process(callback))

        self.assertEqual(status, AckMessage.STATUS_OK)
        self.assertEqual(
            response["cardData"]["cardParamMap"]["lastAction"],
            "generate_closure",
        )
        callback_card = json.dumps(
            response["cardData"]["cardParamMap"], ensure_ascii=False
        )
        self.assertIn("结案草稿", callback_card)
        self.assertIn("确认并结案", callback_card)
        self.assertEqual(closure.commands[0]["text"], "总结并结案")

    def test_continue_callback_keeps_complete_button_visible(self):
        state = InMemoryDingTalkState()
        interaction = create_interaction(state)
        service = DingTalkCollaborationCardService(
            state=state,
            closure=RecordingClosure(),
            messenger=RecordingCardMessenger(),
            operator_user_ids=("operator-1",),
        )
        handler = DingTalkCollaborationCardHandler(service=service)
        callback = SimpleNamespace(
            data={
                "outTrackId": (
                    f"internal-service-closure-v2-{interaction.ticket_code}"
                ),
                "userId": "operator-1",
                "content": json.dumps(
                    {"action": {"componentId": "continue_dialogue"}}
                ),
            }
        )

        status, response = asyncio.run(handler.process(callback))

        self.assertEqual(status, AckMessage.STATUS_OK)
        callback_card = json.dumps(
            response["cardData"]["cardParamMap"], ensure_ascii=False
        )
        self.assertIn("继续沟通中", callback_card)
        self.assertIn("完成并沉淀", callback_card)
        self.assertEqual(
            response["cardData"]["cardParamMap"]["flowStatus"],
            "3",
        )


if __name__ == "__main__":
    unittest.main()
