import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.dingtalk_adapter import DingTalkInboundMessage, InMemoryDingTalkState
from app.dingtalk_collaboration import (
    CollaborationGroup,
    CollaborationGroupError,
    DingTalkCollaborationService,
    DwsCollaborationGroupGateway,
)
from app.enterprise_assistant import EnterpriseAnswer


class RecordingGroupGateway:
    def __init__(self) -> None:
        self.created = []
        self.added_bots = []
        self.messages = []

    def create_group(self, *, name, member_user_ids):
        self.created.append((name, tuple(member_user_ids)))
        return CollaborationGroup(group_id=f"group-{len(self.created)}", name=name)

    def add_bot(self, group_id):
        self.added_bots.append(group_id)

    def send_text(self, group_id, text, *, title):
        self.messages.append((group_id, title, text))


def create_handoff(
    state: InMemoryDingTalkState,
    *,
    message_id: str,
    text: str,
    channel: str = "direct",
):
    return state.record_interaction(
        message=DingTalkInboundMessage(
            message_id=message_id,
            sender_id="employee-1",
            conversation_id="direct-1",
            text=text,
            channel=channel,
        ),
        answer=EnterpriseAnswer(
            domain="shareholder",
            action="handoff",
            text="现有知识不足。",
            reason="knowledge_gap",
        ),
    )


class DingTalkCollaborationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = InMemoryDingTalkState()
        self.gateway = RecordingGroupGateway()
        self.service = DingTalkCollaborationService(
            state=self.state,
            gateway=self.gateway,
            handler_user_ids_by_domain={"shareholder": "owner-1"},
            operator_user_ids=("operator-1",),
        )

    def test_creates_group_adds_bot_and_records_link(self) -> None:
        interaction = create_handoff(
            self.state,
            message_id="message-1",
            text="达到什么条件可以开下一家店？",
        )

        result = self.service.ensure_group(interaction)

        saved = self.state.get_interaction(interaction.ticket_code)
        self.assertFalse(result.reused)
        self.assertEqual(
            self.gateway.created,
            [(result.group_name, ("employee-1", "owner-1", "operator-1"))],
        )
        self.assertEqual(self.gateway.added_bots, ["group-1"])
        _, title, text = self.gateway.messages[0]
        self.assertIn("达到什么条件可以开下一家店", title)
        self.assertIn(interaction.ticket_code, text)
        self.assertIn("**问题概览：**", text)
        self.assertIn("### 完整问题链", text)
        self.assertIn("### 当前需要", text)
        self.assertIn("员工提问：达到什么条件可以开下一家店", text)
        self.assertIn("知识库暂时没有可靠答案", text)
        self.assertIn("请负责人直接在群里与员工沟通", text)
        self.assertNotIn("机器人此前答复", text)
        self.assertNotIn("下一步", text)
        self.assertTrue(text.startswith("## 股东业务｜需要负责人协助"))
        self.assertIn("\n\n", text)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.collaboration_group_id, "group-1")
        self.assertEqual(saved.collaboration_status, "active")

    def test_opening_message_preserves_the_full_clarification_chain(self) -> None:
        interaction = create_handoff(
            self.state,
            message_id="message-chain",
            text=(
                "员工最初提问：我想查7月份的分红，为什么金额不对？\n"
                "机器人追问：请问是哪家门店的7月份分红？\n"
                "员工补充：7月的，没有指定门店。"
            ),
        )

        self.service.ensure_group(interaction)

        _, title, text = self.gateway.messages[0]
        self.assertIn("我想查7月份的分红", title)
        self.assertIn("员工最初提问：我想查7月份的分红", text)
        self.assertIn("机器人追问：请问是哪家门店", text)
        self.assertIn("员工补充：7月的，没有指定门店", text)

    def test_unresolved_feedback_includes_the_previous_robot_answer(self) -> None:
        interaction = self.state.record_interaction(
            message=DingTalkInboundMessage(
                message_id="message-unresolved",
                sender_id="employee-1",
                conversation_id="direct-1",
                text="股东跨城市调店后，原门店分红资格怎么处理？",
                channel="direct",
            ),
            answer=EnterpriseAnswer(
                domain="shareholder",
                action="allow",
                text="原门店分红资格会继续保留。",
            ),
        )
        unresolved = self.state.record_feedback(
            sender_id="employee-1",
            dingtalk_conversation_id="direct-1",
            feedback="unresolved",
        )
        self.assertIsNotNone(unresolved)

        self.service.ensure_group(unresolved)

        _, _, text = self.gateway.messages[0]
        self.assertIn("员工提问：股东跨城市调店后", text)
        self.assertIn("机器人此前答复：原门店分红资格会继续保留", text)
        self.assertIn("员工反馈：上述答复没有解决问题", text)

    def test_creates_separate_groups_for_separate_tickets(self) -> None:
        first = create_handoff(
            self.state,
            message_id="message-1",
            text="第一个问题",
        )
        self.service.ensure_group(first)
        second = create_handoff(
            self.state,
            message_id="message-2",
            text="继续追问一个相关问题",
        )

        result = self.service.ensure_group(second)

        saved = self.state.get_interaction(second.ticket_code)
        self.assertFalse(result.reused)
        self.assertEqual(len(self.gateway.created), 2)
        self.assertEqual(len(self.gateway.messages), 2)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.collaboration_group_id, "group-2")
        self.assertIn(second.ticket_code, saved.collaboration_group_name)

    def test_reuses_only_the_group_already_attached_to_the_same_ticket(self) -> None:
        interaction = create_handoff(
            self.state,
            message_id="message-1",
            text="同一个问题",
        )
        first = self.service.ensure_group(interaction)

        repeated = self.service.ensure_group(interaction)

        self.assertTrue(repeated.reused)
        self.assertEqual(repeated.group_id, first.group_id)
        self.assertEqual(len(self.gateway.created), 1)
        self.assertEqual(len(self.gateway.messages), 1)

    def test_does_not_create_nested_group_from_group_message(self) -> None:
        interaction = create_handoff(
            self.state,
            message_id="message-group",
            text="群里继续追问",
            channel="group",
        )

        with self.assertRaisesRegex(CollaborationGroupError, "Nested"):
            self.service.ensure_group(interaction)

        self.assertEqual(self.gateway.created, [])

    def test_requires_a_department_owner(self) -> None:
        service = DingTalkCollaborationService(
            state=self.state,
            gateway=self.gateway,
            handler_user_ids_by_domain={},
        )
        interaction = create_handoff(
            self.state,
            message_id="message-owner",
            text="谁负责处理？",
        )

        with self.assertRaisesRegex(CollaborationGroupError, "No department owner"):
            service.ensure_group(interaction)


class DwsCollaborationGroupGatewayTests(unittest.TestCase):
    @patch("app.dingtalk_collaboration.subprocess.run")
    def test_resolves_user_id_to_history_sender_open_id(self, run) -> None:
        run.side_effect = [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": [
                            {
                                "orgEmployeeModel": {
                                    "orgUserId": "owner-1",
                                    "orgUserName": "负责人",
                                }
                            }
                        ]
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": [
                            {
                                "name": "负责人",
                                "userId": "owner-1",
                                "openDingTalkId": "owner-open-1",
                            }
                        ]
                    }
                ),
                stderr="",
            ),
        ]
        gateway = DwsCollaborationGroupGateway(
            robot_code="robot-1",
            profile="corp:user",
        )

        aliases = gateway.resolve_sender_aliases(("owner-1",))

        self.assertEqual(aliases, {"owner-1": "owner-open-1"})
        directory_command = run.call_args_list[0].args[0]
        self.assertEqual(
            directory_command[directory_command.index("--ids") + 1],
            "owner-1",
        )
        command = run.call_args_list[1].args[0]
        self.assertEqual(
            command[command.index("--query") + 1],
            "负责人",
        )
        self.assertIn("--format", command)

    @patch("app.dingtalk_collaboration.subprocess.run")
    def test_uses_structured_dws_commands_and_parses_group_id(self, run) -> None:
        run.side_effect = [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"result": {"openConversationId": "conversation-1"}}
                ),
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="{}", stderr=""),
            SimpleNamespace(returncode=0, stdout="{}", stderr=""),
        ]
        gateway = DwsCollaborationGroupGateway(
            robot_code="robot-1",
            profile="corp:user",
        )

        group = gateway.create_group(
            name="测试协作群",
            member_user_ids=("employee-1", "owner-1"),
        )
        gateway.add_bot(group.group_id)
        gateway.send_text(group.group_id, "问题摘要", title="待确认：开店资格")

        self.assertEqual(group.group_id, "conversation-1")
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("--format", command)
            self.assertIn("json", command)
            self.assertIn("--profile", command)
            self.assertNotIsInstance(command, str)
        self.assertIn("add-bot", run.call_args_list[1].args[0])
        self.assertIn("send-by-bot", run.call_args_list[2].args[0])
        send_command = run.call_args_list[2].args[0]
        self.assertEqual(send_command[send_command.index("--title") + 1], "待确认：开店资格")
        self.assertEqual(send_command[send_command.index("--text") + 1], "问题摘要")

    @patch("app.dingtalk_collaboration.subprocess.run")
    def test_bypasses_windows_cmd_wrapper_for_multiline_messages(self, run) -> None:
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="{}",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            node_modules = Path(directory)
            wrapper = node_modules / ".bin" / "dws.cmd"
            executable = (
                node_modules
                / "dingtalk-workspace-cli"
                / "vendor"
                / "dws.exe"
            )
            wrapper.parent.mkdir(parents=True)
            executable.parent.mkdir(parents=True)
            wrapper.touch()
            executable.touch()
            gateway = DwsCollaborationGroupGateway(
                robot_code="robot-1",
                executable=str(wrapper),
            )

            gateway.send_text(
                "group-1",
                "第一段\n\n第二段",
                title="完整消息",
            )

        command = run.call_args.args[0]
        self.assertEqual(command[0], str(executable.resolve()))
        self.assertEqual(command[command.index("--text") + 1], "第一段\n\n第二段")


if __name__ == "__main__":
    unittest.main()
