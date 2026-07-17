import unittest

from app.chatwoot_events import HumanReply, IncomingMessage
from app.inbound_queue import InboundQueue
from app.job_models import EnqueueResult


class FakeJobStore:
    def __init__(self, result: EnqueueResult) -> None:
        self.result = result
        self.messages: list[IncomingMessage] = []
        self.human_replies: list[HumanReply] = []

    def enqueue(self, message: IncomingMessage) -> EnqueueResult:
        self.messages.append(message)
        return self.result

    def record_human_reply(self, reply: HumanReply) -> int:
        self.human_replies.append(reply)
        return 1


class FakeOperationsRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[dict[str, object]] = []

    def record_event(self, **values: object) -> None:
        if self.fail:
            raise RuntimeError("metrics unavailable")
        self.events.append(values)

    def record_knowledge_gap(self, **values: object) -> None:
        return None


def incoming_message() -> IncomingMessage:
    return IncomingMessage(
        message_id=101,
        account_id=1,
        conversation_id=42,
        sender_id=9,
        content="现货多久发货？",
        labels=(),
    )


class InboundQueueTests(unittest.TestCase):
    def test_new_message_is_persisted_without_recording_final_metric(self) -> None:
        store = FakeJobStore(EnqueueResult(status="queued", job_id=1))
        operations = FakeOperationsRecorder()
        queue = InboundQueue(store, operations)

        result = queue.enqueue(incoming_message())

        self.assertEqual(result.status, "queued")
        self.assertEqual(store.messages, [incoming_message()])
        self.assertEqual(operations.events, [])

    def test_duplicate_metric_is_best_effort(self) -> None:
        store = FakeJobStore(EnqueueResult(status="duplicate", job_id=None))
        operations = FakeOperationsRecorder(fail=True)
        queue = InboundQueue(store, operations)

        with self.assertLogs("app.inbound_queue", level="WARNING"):
            result = queue.enqueue(incoming_message())

        self.assertEqual(result.status, "duplicate")
        self.assertEqual(store.messages, [incoming_message()])

    def test_human_reply_is_persisted_without_operations_payload(self) -> None:
        store = FakeJobStore(EnqueueResult(status="queued", job_id=1))
        operations = FakeOperationsRecorder()
        queue = InboundQueue(store, operations)
        reply = HumanReply(
            message_id=102,
            account_id=1,
            conversation_id=42,
            created_at=1_000,
        )

        queue.record_human_reply(reply)

        self.assertEqual(store.human_replies, [reply])
        self.assertEqual(operations.events, [])


if __name__ == "__main__":
    unittest.main()
