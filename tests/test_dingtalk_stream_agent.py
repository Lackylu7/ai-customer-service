import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dingtalk_stream import AckMessage

from app.dingtalk_stream_agent import (
    DingTalkEnterpriseHandler,
    DingTalkStreamSettings,
)
from app.dingtalk_adapter import (
    DingTalkAssistantAdapter,
    InMemoryDingTalkState,
)
from app.enterprise_assistant import EnterpriseAnswer, EnterpriseAssistant
from app.dingtalk_collaboration import CollaborationResult


class RecordingAdapter:
    def __init__(self) -> None:
        self.messages = []

    def handle(self, message):
        self.messages.append(message)
        return EnterpriseAnswer(
            domain="hr",
            action="allow",
            text="\u8bf7\u6309\u5165\u804c\u6e05\u5355\u529e\u7406\u3002",
            conversation_id="dify-conversation-1",
        )


class RecordingHandler(DingTalkEnterpriseHandler):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.replies = []

    def reply_text(self, text, incoming_message):
        self.replies.append((text, incoming_message.message_id))
        return {"success": True}


class FailingBrain:
    def send_message(self, *, query, user, conversation_id=None):
        del query, user, conversation_id
        raise RuntimeError("backend unavailable")


class HandoffAdapter:
    def __init__(self, state) -> None:
        self.state = state

    def handle(self, message):
        answer = EnterpriseAnswer(
            domain="shareholder",
            action="handoff",
            text="现有知识不足。",
            reason="knowledge_gap",
        )
        self.state.record_interaction(message=message, answer=answer)
        self.state.mark_processed(message.message_id)
        return answer


class RecordingWorkOrders:
    def __init__(self) -> None:
        self.notifications = []

    def handle_command(self, **kwargs):
        del kwargs
        return None

    def notify_handlers(self, interaction):
        self.notifications.append(interaction.ticket_code)


class RecordingCollaborations:
    def __init__(self) -> None:
        self.interactions = []

    def ensure_group(self, interaction):
        self.interactions.append(interaction)
        return CollaborationResult(
            group_id="group-1",
            group_name="内部服务协作群",
            reused=False,
            employee_message="已创建协作群，请在群内继续沟通。",
        )


class RecordingCollaborationCards:
    def __init__(self) -> None:
        self.sent = []

    def send_open_card(self, **kwargs):
        self.sent.append(kwargs)
        return f"card-{kwargs['interaction'].ticket_code}"


class RecordingCollaborationClosure:
    def __init__(self, *, command_result="已生成结案草稿。", active_group=True):
        self.commands = []
        self.command_result = command_result
        self.active_group = active_group

    def handle_group_command(self, **kwargs):
        self.commands.append(kwargs)
        return self.command_result

    def record_group_message(self, **kwargs):
        self.recorded_message = kwargs
        return True

    def is_active_group(self, *, group_id):
        return self.active_group and group_id == "conversation-1"


class RecordingProjectOperator:
    def __init__(self):
        self.calls = []

    def handle(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text="机器人服务：运行正常")


def callback_data(*, sender_staff_id: str = "staff-1") -> dict:
    return {
        "msgId": "message-1",
        "senderId": "sender-open-id",
        "senderStaffId": sender_staff_id,
        "chatbotUserId": "bot-open-id",
        "conversationId": "conversation-1",
        "conversationType": "1",
        "msgtype": "text",
        "text": {"content": "\u5165\u804c\u8981\u529e\u4ec0\u4e48\uff1f"},
        "sessionWebhook": "https://example.invalid/session",
    }


class DingTalkStreamSettingsTests(unittest.TestCase):
    def test_windows_start_script_is_ascii_safe(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "start-dingtalk-native-stream.ps1"
        ).read_bytes()

        text = script.decode("ascii")
        self.assertIn("[char]0x5185", text)
        self.assertIn("[char]0x4F5C", text)

    def test_requires_credentials_and_allowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "allowed user list"):
            DingTalkStreamSettings.from_env(
                {
                    "DINGTALK_CLIENT_ID": "client-id",
                    "DINGTALK_CLIENT_SECRET": "secret",
                }
            )

    def test_parses_allowlist_without_exposing_secret_in_repr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = DingTalkStreamSettings.from_env(
                {
                    "DINGTALK_CLIENT_ID": "client-id",
                    "DINGTALK_CLIENT_SECRET": "private-secret",
                    "DINGTALK_ALLOWED_USERS": "staff-1, staff-2",
                    "DINGTALK_STATE_DB_PATH": str(Path(directory) / "state.db"),
                }
            )

        self.assertEqual(settings.allowed_user_ids, {"staff-1", "staff-2"})
        self.assertEqual(settings.handler_user_ids, frozenset())
        self.assertEqual(settings.operator_user_ids, frozenset())
        self.assertEqual(settings.domain_handler_user_ids, {})
        self.assertEqual(settings.robot_code, "client-id")
        self.assertFalse(settings.auto_group_enabled)
        self.assertFalse(settings.project_operator_enabled)
        self.assertEqual(settings.dws_executable, "dws")
        self.assertIsNone(settings.dws_profile)
        self.assertNotIn("private-secret", repr(settings))

    def test_requires_handlers_to_be_allowlisted(self) -> None:
        with self.assertRaisesRegex(ValueError, "handlers"):
            DingTalkStreamSettings.from_env(
                {
                    "DINGTALK_CLIENT_ID": "client-id",
                    "DINGTALK_CLIENT_SECRET": "secret",
                    "DINGTALK_ALLOWED_USERS": "staff-1",
                    "DINGTALK_HANDLER_USERS": "staff-2",
                }
            )

    def test_parses_one_department_owner_per_business_domain(self) -> None:
        settings = DingTalkStreamSettings.from_env(
            {
                "DINGTALK_CLIENT_ID": "client-id",
                "DINGTALK_CLIENT_SECRET": "secret",
                "DINGTALK_ALLOWED_USERS": "staff-1,staff-2",
                "DINGTALK_HANDLER_USERS": "staff-1,staff-2",
                "DINGTALK_DOMAIN_HANDLERS": (
                    "shareholder=staff-1,finance=staff-2"
                ),
            }
        )

        self.assertEqual(
            settings.domain_handler_user_ids,
            {"shareholder": "staff-1", "finance": "staff-2"},
        )

    def test_department_owner_must_be_an_allowlisted_handler(self) -> None:
        with self.assertRaisesRegex(ValueError, "department owners"):
            DingTalkStreamSettings.from_env(
                {
                    "DINGTALK_CLIENT_ID": "client-id",
                    "DINGTALK_CLIENT_SECRET": "secret",
                    "DINGTALK_ALLOWED_USERS": "staff-1,staff-2",
                    "DINGTALK_HANDLER_USERS": "staff-1",
                    "DINGTALK_DOMAIN_HANDLERS": "shareholder=staff-2",
                }
            )

    def test_project_operator_must_be_an_allowlisted_handler(self) -> None:
        with self.assertRaisesRegex(ValueError, "project operators"):
            DingTalkStreamSettings.from_env(
                {
                    "DINGTALK_CLIENT_ID": "client-id",
                    "DINGTALK_CLIENT_SECRET": "secret",
                    "DINGTALK_ALLOWED_USERS": "staff-1,staff-2",
                    "DINGTALK_HANDLER_USERS": "staff-1",
                    "DINGTALK_OPERATOR_USERS": "staff-2",
                }
            )

    def test_parses_project_operator(self) -> None:
        settings = DingTalkStreamSettings.from_env(
            {
                "DINGTALK_CLIENT_ID": "client-id",
                "DINGTALK_CLIENT_SECRET": "secret",
                "DINGTALK_ALLOWED_USERS": "staff-1,staff-2",
                "DINGTALK_HANDLER_USERS": "staff-1,staff-2",
                "DINGTALK_OPERATOR_USERS": "staff-2",
                "OPENCLAW_PROJECT_OPERATOR_ENABLED": "true",
            }
        )

        self.assertEqual(settings.operator_user_ids, frozenset({"staff-2"}))
        self.assertTrue(settings.project_operator_enabled)

    def test_enabled_project_operator_requires_an_operator_user(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires at least one"):
            DingTalkStreamSettings.from_env(
                {
                    "DINGTALK_CLIENT_ID": "client-id",
                    "DINGTALK_CLIENT_SECRET": "secret",
                    "DINGTALK_ALLOWED_USERS": "staff-1",
                    "OPENCLAW_PROJECT_OPERATOR_ENABLED": "true",
                }
            )

    def test_automatic_groups_require_department_owner_routing(self) -> None:
        with self.assertRaisesRegex(ValueError, "department owner routing"):
            DingTalkStreamSettings.from_env(
                {
                    "DINGTALK_CLIENT_ID": "client-id",
                    "DINGTALK_CLIENT_SECRET": "secret",
                    "DINGTALK_ALLOWED_USERS": "staff-1",
                    "DINGTALK_HANDLER_USERS": "staff-1",
                    "DINGTALK_AUTO_GROUP_ENABLED": "true",
                }
            )


class DingTalkEnterpriseHandlerTests(unittest.TestCase):
    def test_project_operator_command_is_processed_before_customer_answering(self):
        state = InMemoryDingTalkState()
        adapter = RecordingAdapter()
        operator = RecordingProjectOperator()
        handler = RecordingHandler(
            adapter=adapter,
            allowed_user_ids=frozenset({"staff-1"}),
            state=state,
            project_operator=operator,
        )
        data = callback_data()
        data["text"] = {"content": "龙虾：检查机器人状态"}

        status, message = asyncio.run(handler.process(SimpleNamespace(data=data)))

        self.assertEqual(
            (status, message),
            (AckMessage.STATUS_OK, "project operator command"),
        )
        self.assertEqual(adapter.messages, [])
        self.assertTrue(state.is_processed("message-1"))
        self.assertEqual(handler.replies[0][0], "机器人服务：运行正常")

    def test_group_closure_command_is_processed_before_normal_answering(self) -> None:
        adapter = RecordingAdapter()
        closure = RecordingCollaborationClosure()
        handler = RecordingHandler(
            adapter=adapter,
            allowed_user_ids=frozenset({"staff-1"}),
            collaboration_closure=closure,
        )
        data = callback_data()
        data["conversationType"] = "2"
        data["text"] = {"content": "总结并结案"}

        status, message = asyncio.run(handler.process(SimpleNamespace(data=data)))

        self.assertEqual(
            (status, message),
            (AckMessage.STATUS_OK, "collaboration closure command"),
        )
        self.assertEqual(adapter.messages, [])
        self.assertEqual(handler.replies[0][0], "已生成结案草稿。")

    def test_active_collaboration_group_message_does_not_create_new_ticket(self) -> None:
        state = InMemoryDingTalkState()
        adapter = RecordingAdapter()
        closure = RecordingCollaborationClosure(command_result=None)
        handler = RecordingHandler(
            adapter=adapter,
            allowed_user_ids=frozenset({"staff-1"}),
            state=state,
            collaboration_closure=closure,
        )
        data = callback_data()
        data["conversationType"] = "2"
        data["text"] = {"content": "结束"}

        status, message = asyncio.run(handler.process(SimpleNamespace(data=data)))

        self.assertEqual(
            (status, message),
            (AckMessage.STATUS_OK, "collaboration message collected"),
        )
        self.assertEqual(adapter.messages, [])
        self.assertEqual(handler.replies, [])
        self.assertTrue(state.is_processed("message-1"))
        self.assertEqual(closure.recorded_message["message_id"], "message-1")
        self.assertEqual(closure.recorded_message["sender_id"], "staff-1")
        self.assertEqual(closure.recorded_message["content"], "结束")

    def test_non_collaboration_group_message_keeps_normal_answering(self) -> None:
        adapter = RecordingAdapter()
        closure = RecordingCollaborationClosure(
            command_result=None,
            active_group=False,
        )
        handler = RecordingHandler(
            adapter=adapter,
            allowed_user_ids=frozenset({"staff-1"}),
            collaboration_closure=closure,
        )
        data = callback_data()
        data["conversationType"] = "2"

        status, message = asyncio.run(handler.process(SimpleNamespace(data=data)))

        self.assertEqual((status, message), (AckMessage.STATUS_OK, "OK"))
        self.assertEqual(len(adapter.messages), 1)

    def test_maps_sdk_metadata_and_replies(self) -> None:
        adapter = RecordingAdapter()
        handler = RecordingHandler(
            adapter=adapter,
            allowed_user_ids=frozenset({"staff-1"}),
        )

        status, message = asyncio.run(
            handler.process(SimpleNamespace(data=callback_data()))
        )

        self.assertEqual((status, message), (AckMessage.STATUS_OK, "OK"))
        self.assertEqual(len(adapter.messages), 1)
        inbound = adapter.messages[0]
        self.assertEqual(inbound.message_id, "message-1")
        self.assertEqual(inbound.sender_id, "staff-1")
        self.assertEqual(inbound.conversation_id, "conversation-1")
        self.assertEqual(inbound.channel, "direct")
        self.assertEqual(
            handler.replies,
            [("\u8bf7\u6309\u5165\u804c\u6e05\u5355\u529e\u7406\u3002", "message-1")],
        )

    def test_ignores_non_allowlisted_sender(self) -> None:
        adapter = RecordingAdapter()
        handler = RecordingHandler(
            adapter=adapter,
            allowed_user_ids=frozenset({"staff-1"}),
        )

        status, message = asyncio.run(
            handler.process(
                SimpleNamespace(data=callback_data(sender_staff_id="staff-2"))
            )
        )

        self.assertEqual((status, message), (AckMessage.STATUS_OK, "ignored"))
        self.assertEqual(adapter.messages, [])
        self.assertEqual(handler.replies, [])

    def test_backend_failure_is_recorded_as_a_human_review_item(self) -> None:
        state = InMemoryDingTalkState()
        handler = RecordingHandler(
            adapter=DingTalkAssistantAdapter(
                assistant=EnterpriseAssistant(brain=FailingBrain()),
                state=state,
            ),
            allowed_user_ids=frozenset({"staff-1"}),
        )

        with self.assertLogs("dingtalk-stream", level="ERROR"):
            status, message = asyncio.run(
                handler.process(SimpleNamespace(data=callback_data()))
            )

        interactions = state.list_interactions()
        self.assertEqual((status, message), (AckMessage.STATUS_OK, "OK"))
        self.assertEqual(len(interactions), 1)
        self.assertEqual(interactions[0].status, "needs_human")
        self.assertEqual(interactions[0].reason, "system_unavailable")
        self.assertIn(interactions[0].ticket_code, handler.replies[0][0])

    def test_handoff_opens_collaboration_group_before_falling_back_to_work_order(
        self,
    ) -> None:
        state = InMemoryDingTalkState()
        work_orders = RecordingWorkOrders()
        collaborations = RecordingCollaborations()
        cards = RecordingCollaborationCards()
        handler = RecordingHandler(
            adapter=HandoffAdapter(state),
            allowed_user_ids=frozenset({"staff-1"}),
            state=state,
            work_orders=work_orders,
            collaborations=collaborations,
            collaboration_cards=cards,
        )

        status, message = asyncio.run(
            handler.process(SimpleNamespace(data=callback_data()))
        )

        self.assertEqual((status, message), (AckMessage.STATUS_OK, "OK"))
        self.assertEqual(len(collaborations.interactions), 1)
        self.assertEqual(work_orders.notifications, [])
        self.assertEqual(len(cards.sent), 1)
        self.assertEqual(cards.sent[0]["group_id"], "group-1")
        self.assertEqual(
            handler.replies[0][0],
            "已创建协作群，请在群内继续沟通。",
        )


if __name__ == "__main__":
    unittest.main()
