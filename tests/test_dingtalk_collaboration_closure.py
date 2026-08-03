import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.dify_chat_client import DifyChatResponse
from app.dify_client import CreatedDocument
from app.dingtalk_adapter import DingTalkInboundMessage, SQLiteDingTalkState
from app.dingtalk_collaboration import (
    CollaborationMessage,
    DwsCollaborationGroupGateway,
)
from app.dingtalk_collaboration_closure import (
    BrainCollaborationSummarizer,
    CollaborationDraft,
    DifyCollaborationKnowledgePublisher,
    DingTalkCollaborationClosureService,
    SQLiteCollaborationTranscriptStore,
)
from app.enterprise_assistant import EnterpriseAnswer


class RecordingTranscriptGateway:
    def __init__(self):
        self.calls = []

    def list_messages(self, *, group_id, since):
        self.calls.append((group_id, since))
        return (
            CollaborationMessage(
                message_id="message-1",
                sender_id="employee-1",
                sender_name="员工",
                content="我想知道开下一家店的条件。",
                created_at=since + 10,
            ),
            CollaborationMessage(
                message_id="message-2",
                sender_id="owner-1",
                sender_name="负责人",
                content="需要先完成门店经营指标核实，以负责人最终确认为准。",
                created_at=since + 20,
            ),
        )


class PermissionDeniedTranscriptGateway:
    def list_messages(self, *, group_id, since):
        del group_id, since
        from app.dingtalk_collaboration import CollaborationGroupError

        raise CollaborationGroupError("permission denied")


class OpenIdentityTranscriptGateway:
    def list_messages(self, *, group_id, since):
        del group_id
        return (
            CollaborationMessage(
                message_id="history-owner-answer",
                sender_id="owner-open-1",
                sender_name="负责人",
                content="核实后的答复是先达到经营指标再申请。",
                created_at=since + 10,
            ),
        )


class NoiseOnlyTranscriptGateway:
    def list_messages(self, *, group_id, since):
        del group_id
        return (
            CollaborationMessage(
                message_id="owner-greeting",
                sender_id="owner-1",
                sender_name="负责人",
                content="喂",
                created_at=since + 10,
            ),
            CollaborationMessage(
                message_id="operator-technical-test",
                sender_id="operator-1",
                sender_name="项目管理员",
                content="【技术测试，非业务答复】请勿作为结案依据。",
                created_at=since + 20,
            ),
        )


class SummaryBrain:
    def send_message(self, *, query, user, conversation_id=None):
        del query, user, conversation_id
        return DifyChatResponse(
            answer=json.dumps(
                {
                    "final_answer": "需要先完成门店经营指标核实。",
                    "root_cause": "原资料未说明核实入口。",
                    "knowledge_candidate": "开下一家店前需完成经营指标核实。",
                },
                ensure_ascii=False,
            ),
            conversation_id="summary-1",
            retriever_resources=(),
        )


class RecordingKnowledgePublisher:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("Dify unavailable")
        return CreatedDocument(
            document_id="knowledge-1",
            batch="batch-1",
            indexing_status="indexing",
        )


class RecordingDifyDatasetClient:
    def __init__(self):
        self.calls = []

    def create_document_by_text(self, **kwargs):
        self.calls.append(kwargs)
        return CreatedDocument(
            document_id="document-1",
            batch="batch-1",
            indexing_status="indexing",
        )


def create_collaboration(state):
    interaction = state.record_interaction(
        message=DingTalkInboundMessage(
            message_id="question-1",
            sender_id="employee-1",
            conversation_id="direct-1",
            text="开下一家店需要什么条件？",
            channel="direct",
        ),
        answer=EnterpriseAnswer(
            domain="shareholder",
            action="handoff",
            text="现有知识不足。",
            reason="knowledge_gap",
        ),
    )
    return state.attach_collaboration_group(
        ticket_code=interaction.ticket_code,
        group_id="group-1",
        group_name="股东业务协作",
    )


class CollaborationClosureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database = Path(self.directory.name) / "state.db"
        self.state = SQLiteDingTalkState(database)
        self.interaction = create_collaboration(self.state)
        self.gateway = RecordingTranscriptGateway()
        self.store = SQLiteCollaborationTranscriptStore(database)
        self.publisher = RecordingKnowledgePublisher()
        self.service = DingTalkCollaborationClosureService(
            state=self.state,
            gateway=self.gateway,
            transcript_store=self.store,
            summarizer=BrainCollaborationSummarizer(SummaryBrain()),
            handler_user_ids_by_domain={"shareholder": "owner-1"},
            operator_user_ids=("operator-1",),
            knowledge_publisher=self.publisher,
        )

    def test_owner_can_complete_and_publish_in_one_action(self):
        result = self.service.handle_group_command(
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            text="完成并沉淀",
        )

        resolved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIn("已结案并已沉淀到正式知识库", result)
        self.assertIn("knowledge-1", result)
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.knowledge_status, "approved")
        self.assertEqual(len(self.publisher.calls), 1)

    def test_publish_failure_keeps_interaction_open(self):
        self.service._knowledge_publisher = RecordingKnowledgePublisher(fail=True)

        result = self.service.handle_group_command(
            sender_id="operator-1",
            sender_name="项目管理员",
            group_id="group-1",
            text="完成并沉淀",
        )

        pending = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIn("写入失败", result)
        self.assertEqual(pending.status, "needs_human")
        self.assertEqual(pending.knowledge_status, "not_reviewed")

    def test_summary_is_only_a_draft_until_owner_confirms(self):
        preview = self.service.handle_group_command(
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            text="总结并结案",
        )

        pending = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIn("结案草稿", preview)
        self.assertEqual(pending.status, "needs_human")
        self.assertEqual(pending.knowledge_status, "not_reviewed")
        self.assertEqual(
            len(self.store.list_messages(ticket_code=self.interaction.ticket_code)),
            2,
        )

        result = self.service.handle_group_command(
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            text="确认结案",
        )

        resolved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIn("已结案", result)
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.knowledge_status, "approved")
        self.assertEqual(resolved.collaboration_status, "resolved")

    def test_non_owner_cannot_close_the_group(self):
        result = self.service.handle_group_command(
            sender_id="employee-1",
            sender_name="员工",
            group_id="group-1",
            text="总结并结案",
        )

        self.assertEqual(
            result,
            "只有该业务板块负责人或项目管理员可以总结或确认结案。",
        )
        self.assertEqual(self.gateway.calls, [])

    def test_project_operator_can_generate_and_confirm_closure(self):
        preview = self.service.handle_group_command(
            sender_id="operator-1",
            sender_name="项目管理员",
            group_id="group-1",
            text="总结并结案",
        )
        result = self.service.handle_group_command(
            sender_id="operator-1",
            sender_name="项目管理员",
            group_id="group-1",
            text="确认结案",
        )

        resolved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIn("结案草稿", preview)
        self.assertIn("已结案", result)
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.owner_id, "operator-1")
        self.assertEqual(resolved.reviewed_by, "项目管理员")

    def test_repeated_sync_deduplicates_messages(self):
        for _ in range(2):
            self.service.handle_group_command(
                sender_id="owner-1",
                sender_name="负责人",
                group_id="group-1",
                text="总结并结案",
            )

        self.assertEqual(
            len(self.store.list_messages(ticket_code=self.interaction.ticket_code)),
            2,
        )

    def test_stream_message_is_persisted_for_active_group(self):
        recorded = self.service.record_group_message(
            message_id="stream-message-1",
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            content="核实后的答复是可以按现行制度申请。",
            created_at=1_785_000_000,
        )

        messages = self.store.list_messages(
            ticket_code=self.interaction.ticket_code
        )
        self.assertTrue(recorded)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, "stream-message-1")
        self.assertEqual(messages[0].sender_id, "owner-1")

    def test_complete_uses_local_stream_messages_when_history_is_denied(self):
        self.service._gateway = PermissionDeniedTranscriptGateway()
        self.service.record_group_message(
            message_id="stream-owner-answer",
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            content="核实后的答复是先达到经营指标再申请。",
            created_at=1_785_000_000,
        )

        result = self.service.handle_group_command(
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            text="完成并沉淀",
        )

        resolved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIn("knowledge-1", result)
        self.assertEqual(resolved.status, "resolved")

    def test_history_open_identity_alias_is_treated_as_authoritative(self):
        service = DingTalkCollaborationClosureService(
            state=self.state,
            gateway=OpenIdentityTranscriptGateway(),
            transcript_store=self.store,
            summarizer=BrainCollaborationSummarizer(SummaryBrain()),
            handler_user_ids_by_domain={"shareholder": "owner-1"},
            sender_aliases_by_user_id={"owner-1": "owner-open-1"},
            knowledge_publisher=self.publisher,
        )

        result = service.handle_group_command(
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            text="完成并沉淀",
        )

        resolved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIn("knowledge-1", result)
        self.assertEqual(resolved.status, "resolved")

    def test_short_chatter_and_technical_markers_cannot_trigger_publication(self):
        service = DingTalkCollaborationClosureService(
            state=self.state,
            gateway=NoiseOnlyTranscriptGateway(),
            transcript_store=self.store,
            summarizer=BrainCollaborationSummarizer(SummaryBrain()),
            handler_user_ids_by_domain={"shareholder": "owner-1"},
            operator_user_ids=("operator-1",),
            knowledge_publisher=self.publisher,
        )

        result = service.handle_group_command(
            sender_id="operator-1",
            sender_name="项目管理员",
            group_id="group-1",
            text="完成并沉淀",
        )

        pending = self.state.get_interaction(self.interaction.ticket_code)
        self.assertIn("还没有识别到", result)
        self.assertEqual(pending.status, "needs_human")
        self.assertEqual(self.publisher.calls, [])

    def test_owner_can_replace_draft_before_confirmation(self):
        self.service.handle_group_command(
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            text="总结并结案",
        )

        preview = self.service.handle_group_command(
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            text="修改结案：请先联系运营负责人完成经营指标核实。",
        )

        self.assertIn("请先联系运营负责人", preview)
        self.service.handle_group_command(
            sender_id="owner-1",
            sender_name="负责人",
            group_id="group-1",
            text="确认结案",
        )
        resolved = self.state.get_interaction(self.interaction.ticket_code)
        self.assertEqual(
            resolved.final_answer,
            "请先联系运营负责人完成经营指标核实。",
        )


class DifyCollaborationKnowledgePublisherTests(unittest.TestCase):
    def test_publishes_redacted_responsible_person_conclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteDingTalkState(Path(directory) / "state.db")
            interaction = state.record_interaction(
                message=DingTalkInboundMessage(
                    message_id="question-sensitive",
                    sender_id="employee-1",
                    conversation_id="direct-1",
                    text="手机号 13800138000 怎么改？",
                    channel="direct",
                ),
                answer=EnterpriseAnswer(
                    domain="shareholder",
                    action="handoff",
                    text="需要负责人核实。",
                    reason="knowledge_gap",
                ),
            )
            client = RecordingDifyDatasetClient()
            publisher = DifyCollaborationKnowledgePublisher(
                dataset_id="dataset-1",
                client=client,
            )

            publisher.publish(
                interaction=interaction,
                draft=CollaborationDraft(
                    ticket_code=interaction.ticket_code,
                    group_id="group-1",
                    final_answer="联系 13900139000 处理。",
                    root_cause="员工不知道修改入口。",
                    knowledge_candidate="联系负责人处理手机号修改。",
                    created_at=1,
                ),
            )

            published = client.calls[0]
            self.assertEqual(published["dataset_id"], "dataset-1")
            self.assertIn("[手机号]", published["text"])
            self.assertNotIn("13800138000", published["text"])
            self.assertNotIn("13900139000", published["text"])


class DwsTranscriptGatewayTests(unittest.TestCase):
    @patch("app.dingtalk_collaboration.subprocess.run")
    def test_lists_messages_with_structured_dws_command(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": {
                        "hasMore": False,
                        "messages": [
                            {
                                "msgId": "message-1",
                                "senderStaffId": "staff-1",
                                "senderNick": "员工",
                                "text": {"content": "测试消息"},
                                "createTime": 1_754_000_000_000,
                            }
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            stderr="",
        )
        gateway = DwsCollaborationGroupGateway(
            robot_code="robot-1",
            profile="corp:user",
        )

        messages = gateway.list_messages(group_id="group-1", since=1_753_999_000)

        self.assertEqual(messages[0].content, "测试消息")
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertIn("list", command)
        self.assertIn("--group", command)
        self.assertIn("--format", command)
        self.assertIn("json", command)

    @patch("app.dingtalk_collaboration.subprocess.run")
    def test_parses_current_dws_message_shape(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "success": True,
                    "result": {
                        "hasMore": False,
                        "messages": [
                            {
                                "content": "负责人已给出核实结论。",
                                "createTime": "2026-07-29 13:18:19",
                                "openMessageId": "message-current-1",
                                "sender": "负责人",
                                "senderOpenDingTalkId": "open-owner-1",
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            stderr="",
        )
        gateway = DwsCollaborationGroupGateway(
            robot_code="robot-1",
            profile="corp:user",
        )

        messages = gateway.list_messages(group_id="group-1", since=1_753_999_000)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, "message-current-1")
        self.assertEqual(messages[0].sender_id, "open-owner-1")
        self.assertEqual(messages[0].sender_name, "负责人")
        self.assertEqual(messages[0].content, "负责人已给出核实结论。")
        self.assertGreater(messages[0].created_at, 0)


if __name__ == "__main__":
    unittest.main()
