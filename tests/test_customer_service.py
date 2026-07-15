import tempfile
import unittest
from pathlib import Path

from app.chatwoot_client import ChatwootAPIError
from app.chatwoot_events import IncomingMessage
from app.conversation_store import SQLiteConversationStore
from app.customer_service import CustomerService
from app.dify_chat_client import DifyChatAPIError, DifyChatResponse
from app.job_models import PreparedAction


class FakeDifyChatClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []
        self.response = DifyChatResponse(
            answer="现货商品通常会在付款后 48 小时内发出。",
            conversation_id="dify-conversation-9",
            retriever_resources=({"document_name": "faq.md"},),
        )
        self.error: Exception | None = None

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
        if self.error is not None:
            raise self.error
        return self.response


class FakeChatwootClient:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []
        self.messages = self.sent_messages
        self.source_ids: list[str | None] = []
        self.existing_source_ids: dict[str, int] = {}
        self.label_calls: list[str] = []
        self.fail_message = False
        self.raise_on_label = False

    def find_outgoing_message_by_source_id(
        self,
        account_id: int,
        conversation_id: int,
        source_id: str,
    ) -> int | None:
        return self.existing_source_ids.get(source_id)

    def send_outgoing_message(
        self,
        account_id: int,
        conversation_id: int,
        content: str,
        *,
        source_id: str | None = None,
    ) -> int:
        if self.fail_message:
            raise ChatwootAPIError(503, "unavailable")
        self.sent_messages.append(content)
        self.source_ids.append(source_id)
        message_id = 501 + len(self.sent_messages)
        if source_id is not None:
            self.existing_source_ids[source_id] = message_id
        return message_id

    def add_label_preserving_existing(
        self,
        account_id: int,
        conversation_id: int,
        label: str,
    ) -> tuple[str, ...]:
        self.label_calls.append(label)
        if self.raise_on_label:
            raise ChatwootAPIError(503, "unavailable")
        return ("vip", label)


class FakeOperationsRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.gaps: list[dict[str, object]] = []
        self.fail = False

    def record_event(self, **event: object) -> None:
        if self.fail:
            raise RuntimeError("operations unavailable")
        self.events.append(event)

    def record_knowledge_gap(self, **gap: object) -> None:
        if self.fail:
            raise RuntimeError("operations unavailable")
        self.gaps.append(gap)


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        current = self.value
        self.value += 0.125
        return current


def incoming(
    content: str,
    *,
    message_id: int = 101,
    conversation_id: int = 9,
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        account_id=1,
        conversation_id=conversation_id,
        sender_id=42,
        content=content,
        labels=("vip",),
    )


class CustomerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.store = SQLiteConversationStore(Path(temp_dir.name) / "state.db")
        self.dify = FakeDifyChatClient()
        self.chatwoot = FakeChatwootClient()
        self.operations = FakeOperationsRecorder()
        self.service = CustomerService(
            store=self.store,
            dify_client=self.dify,
            chatwoot_client=self.chatwoot,
            operations_recorder=self.operations,
            monotonic=FakeMonotonic(),
        )

    def test_prepare_builds_reply_without_chatwoot_side_effect(self) -> None:
        self.dify.response = DifyChatResponse(
            answer="48 小时内发货。",
            conversation_id="dify-7",
            retriever_resources=(),
        )

        action = self.service.prepare(incoming("现货多久发货？"))

        self.assertEqual(action, PreparedAction.reply("48 小时内发货。", "dify-7"))
        self.assertEqual(self.chatwoot.sent_messages, [])
        self.assertIsNone(self.store.get_conversation(9))

    def test_deliver_reuses_existing_source_id_before_saving_reply_state(self) -> None:
        message = incoming("现货多久发货？")
        action = PreparedAction.reply("48 小时内发货。", "dify-7")
        self.chatwoot.existing_source_ids["ai-cs:101:reply"] = 9001

        self.service.deliver(message, action, source_id="ai-cs:101:reply")

        self.assertEqual(self.chatwoot.sent_messages, [])
        state = self.store.get_conversation(message.conversation_id)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual((state.mode, state.dify_conversation_id), ("ai", "dify-7"))

    def test_handoff_label_must_succeed_before_local_human_mode(self) -> None:
        message = incoming("我要人工客服", message_id=102)
        action = PreparedAction.handoff("正在转接人工。", "explicit_human_request")
        self.chatwoot.raise_on_label = True

        with self.assertRaises(ChatwootAPIError):
            self.service.deliver(message, action, source_id="ai-cs:102:reply")

        self.assertIsNone(self.store.get_conversation(message.conversation_id))

    def test_normal_answer_is_sent_and_conversation_is_saved(self) -> None:
        result = self.service.handle(incoming("现货多久发货？"))

        self.assertEqual(result.status, "replied")
        self.assertEqual(self.dify.calls[0]["user"], "chatwoot:42")
        self.assertEqual(self.dify.calls[0]["conversation_id"], None)
        self.assertEqual(
            self.chatwoot.messages,
            ["现货商品通常会在付款后 48 小时内发出。"],
        )
        state = self.store.get_conversation(9)
        self.assertIsNotNone(state)
        self.assertEqual(state.dify_conversation_id, "dify-conversation-9")
        self.assertEqual(state.mode, "ai")
        self.assertEqual(self.operations.events[-1]["outcome"], "replied")
        self.assertIsNone(self.operations.events[-1]["reason"])
        self.assertEqual(self.operations.events[-1]["duration_ms"], 125)
        self.assertEqual(self.operations.gaps, [])

    def test_follow_up_reuses_dify_conversation(self) -> None:
        self.service.handle(incoming("现货多久发货？", message_id=101))
        self.service.handle(incoming("那退货呢？", message_id=102))

        self.assertEqual(
            self.dify.calls[1]["conversation_id"],
            "dify-conversation-9",
        )

    def test_duplicate_message_is_not_processed_twice(self) -> None:
        self.assertEqual(
            self.service.handle(incoming("现货多久发货？")).status,
            "replied",
        )
        duplicate = self.service.handle(incoming("现货多久发货？"))

        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(len(self.dify.calls), 1)
        self.assertEqual(len(self.chatwoot.messages), 1)
        self.assertEqual(
            [event["outcome"] for event in self.operations.events],
            ["replied", "duplicate"],
        )

    def test_human_mode_ignores_future_ai_messages(self) -> None:
        self.store.save_conversation(9, dify_conversation_id=None, mode="human")

        result = self.service.handle(incoming("还有人在吗？"))

        self.assertEqual(result.status, "ignored_human_mode")
        self.assertEqual(self.dify.calls, [])
        self.assertEqual(self.chatwoot.messages, [])
        self.assertEqual(self.operations.events[-1]["outcome"], "ignored_human_mode")

    def test_explicit_human_request_hands_off_without_dify(self) -> None:
        result = self.service.handle(incoming("我要人工客服"))

        self.assertEqual(result.status, "handed_off")
        self.assertEqual(self.dify.calls, [])
        self.assertEqual(
            self.chatwoot.messages,
            ["正在为你转接人工客服，请稍候。"],
        )
        self.assertEqual(self.chatwoot.label_calls, ["ai_handoff"])
        self.assertEqual(self.store.get_conversation(9).mode, "human")
        self.assertEqual(self.operations.events[-1]["outcome"], "handed_off")
        self.assertEqual(
            self.operations.events[-1]["reason"],
            "explicit_human_request",
        )
        self.assertEqual(self.operations.gaps, [])

    def test_dify_handoff_sentinel_uses_safe_customer_text(self) -> None:
        self.dify.response = DifyChatResponse(
            answer="[HANDOFF]当前资料无法确认，正在为你转接人工客服。",
            conversation_id="dify-conversation-9",
            retriever_resources=(),
        )

        result = self.service.handle(incoming("是否终身免费保修？"))

        self.assertEqual(result.status, "handed_off")
        self.assertEqual(
            self.chatwoot.messages,
            ["当前资料无法确认，正在为你转接人工客服。"],
        )
        self.assertNotIn("[HANDOFF]", self.chatwoot.messages[0])
        self.assertEqual(self.operations.events[-1]["reason"], "knowledge_gap")
        self.assertEqual(
            self.operations.gaps,
            [{"question": "是否终身免费保修？", "conversation_id": 9}],
        )

    def test_dify_failure_hands_off_without_exposing_error(self) -> None:
        self.dify.error = DifyChatAPIError(503, "internal model error")

        result = self.service.handle(incoming("现货多久发货？"))

        self.assertEqual(result.status, "handed_off")
        self.assertEqual(
            self.chatwoot.messages,
            ["正在为你转接人工客服，请稍候。"],
        )
        self.assertNotIn("internal model error", self.chatwoot.messages[0])
        self.assertEqual(self.operations.events[-1]["reason"], "dify_error")
        self.assertEqual(self.operations.gaps, [])

    def test_empty_dify_answer_is_system_error_not_knowledge_gap(self) -> None:
        self.dify.response = DifyChatResponse(
            answer="   ",
            conversation_id="dify-conversation-9",
            retriever_resources=(),
        )

        result = self.service.handle(incoming("现货多久发货？"))

        self.assertEqual(result.status, "handed_off")
        self.assertEqual(self.operations.events[-1]["reason"], "dify_error")
        self.assertEqual(self.operations.gaps, [])

    def test_unexpected_processing_error_is_recorded_and_rethrown(self) -> None:
        self.dify.error = RuntimeError("unexpected internal failure")

        with self.assertRaisesRegex(RuntimeError, "unexpected internal failure"):
            self.service.handle(incoming("现货多久发货？"))

        self.assertEqual(self.operations.events[-1]["outcome"], "failed")
        self.assertEqual(self.operations.events[-1]["reason"], "internal_error")

    def test_message_send_failure_releases_claim_for_retry(self) -> None:
        self.chatwoot.fail_message = True
        with self.assertRaises(ChatwootAPIError):
            self.service.handle(incoming("现货多久发货？"))

        self.chatwoot.fail_message = False
        retry = self.service.handle(incoming("现货多久发货？"))

        self.assertEqual(retry.status, "replied")
        self.assertEqual(len(self.dify.calls), 2)
        self.assertEqual(
            [event["outcome"] for event in self.operations.events],
            ["failed", "replied"],
        )
        self.assertEqual(self.operations.events[0]["reason"], "chatwoot_error")

    def test_label_failure_releases_claim_without_switching_local_state(self) -> None:
        self.chatwoot.raise_on_label = True

        with self.assertRaises(ChatwootAPIError):
            self.service.handle(incoming("我要人工客服"))

        self.assertIsNone(self.store.get_conversation(9))
        self.chatwoot.raise_on_label = False
        retry = self.service.handle(incoming("我要人工客服"))
        self.assertEqual(retry.status, "handed_off")
        self.assertEqual(self.store.get_conversation(9).mode, "human")
        self.assertEqual(len(self.chatwoot.sent_messages), 1)

    def test_each_rule_handoff_records_its_specific_reason(self) -> None:
        cases = (
            ("我要人工客服", "explicit_human_request"),
            ("我要投诉到消协", "complaint_or_legal"),
            ("我的账号被盗了", "payment_or_account_security"),
        )

        for index, (content, expected_reason) in enumerate(cases, start=1):
            result = self.service.handle(
                incoming(
                    content,
                    message_id=200 + index,
                    conversation_id=20 + index,
                )
            )
            self.assertEqual(result.status, "handed_off")
            self.assertEqual(self.operations.events[-1]["reason"], expected_reason)
        self.assertEqual(self.operations.gaps, [])

    def test_operations_failure_does_not_change_customer_reply(self) -> None:
        self.operations.fail = True

        with self.assertLogs("app.customer_service", level="WARNING") as logs:
            result = self.service.handle(incoming("现货多久发货？"))

        self.assertEqual(result.status, "replied")
        self.assertEqual(len(self.chatwoot.messages), 1)
        self.assertEqual(self.store.get_conversation(9).mode, "ai")
        self.assertTrue(
            any("Operations event recording failed." in line for line in logs.output)
        )
        self.assertFalse(any("现货多久发货" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
