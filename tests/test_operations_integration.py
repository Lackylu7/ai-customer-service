import tempfile
import unittest
from pathlib import Path

from app.chatwoot_events import IncomingMessage
from app.conversation_store import SQLiteConversationStore
from app.customer_service import CustomerService
from app.dify_chat_client import DifyChatResponse
from app.operations_store import OperationsStore


class AlwaysUnknownDify:
    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse:
        return DifyChatResponse(
            answer="[HANDOFF]当前资料无法确认，正在为你转接人工客服。",
            conversation_id="dify-unknown",
            retriever_resources=(),
        )


class RecordingChatwoot:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_outgoing_message(
        self,
        account_id: int,
        conversation_id: int,
        content: str,
    ) -> int:
        self.messages.append(content)
        return len(self.messages)

    def add_label_preserving_existing(
        self,
        account_id: int,
        conversation_id: int,
        label: str,
    ) -> tuple[str, ...]:
        return (label,)


def incoming(message_id: int, conversation_id: int, content: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        account_id=1,
        conversation_id=conversation_id,
        sender_id=42,
        content=content,
        labels=(),
    )


class CustomerServiceOperationsIntegrationTests(unittest.TestCase):
    def test_normalized_unknown_questions_merge_through_customer_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.db"
            operations = OperationsStore(database_path)
            service = CustomerService(
                store=SQLiteConversationStore(database_path),
                dify_client=AlwaysUnknownDify(),
                chatwoot_client=RecordingChatwoot(),
                operations_recorder=operations,
            )

            first = service.handle(incoming(101, 9, " ＡＢＣ  保修？ "))
            second = service.handle(incoming(102, 10, "abc 保修"))
            gaps, total = operations.list_gaps(status="all")
            summary = operations.get_summary("all")

        self.assertEqual(first.status, "handed_off")
        self.assertEqual(second.status, "handed_off")
        self.assertEqual(total, 1)
        self.assertEqual(gaps[0].occurrence_count, 2)
        self.assertEqual(summary["handoff_reasons"]["knowledge_gap"], 2)


if __name__ == "__main__":
    unittest.main()
