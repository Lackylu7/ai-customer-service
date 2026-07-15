import unittest

from app.chatwoot_client import ChatwootAPIError, ChatwootConversation
from app.chatwoot_reconciliation import ChatwootReconciler
from app.job_models import EnqueueResult


class FakeJobStore:
    def __init__(self, cursor: int | None = None) -> None:
        self.cursor = cursor

    def get_reconciliation_cursor(self) -> int | None:
        return self.cursor

    def initialize_reconciliation_cursor(self, *, now: int) -> bool:
        if self.cursor is not None:
            return False
        self.cursor = now
        return True

    def advance_reconciliation_cursor(
        self,
        *,
        expected: int,
        new_value: int,
    ) -> bool:
        if self.cursor != expected:
            return False
        self.cursor = new_value
        return True


class FakeInboundQueue:
    def __init__(self, duplicate_ids: set[int] | None = None) -> None:
        self.duplicate_ids = duplicate_ids or set()
        self.messages: list[object] = []

    def enqueue(self, message: object) -> EnqueueResult:
        self.messages.append(message)
        if message.message_id in self.duplicate_ids:
            return EnqueueResult(status="duplicate", job_id=None)
        return EnqueueResult(status="queued", job_id=message.message_id)


class FakeChatwootReader:
    def __init__(self) -> None:
        self.pages: dict[int, tuple[ChatwootConversation, ...]] = {}
        self.messages: dict[int, tuple[dict[str, object], ...]] = {}
        self.list_calls: list[tuple[int, int]] = []
        self.message_calls: list[tuple[int, int]] = []
        self.error: Exception | None = None

    def list_conversations_page(
        self,
        account_id: int,
        page: int,
    ) -> tuple[ChatwootConversation, ...]:
        self.list_calls.append((account_id, page))
        if self.error is not None:
            raise self.error
        return self.pages.get(page, ())

    def list_conversation_messages(
        self,
        account_id: int,
        conversation_id: int,
    ) -> tuple[dict[str, object], ...]:
        self.message_calls.append((account_id, conversation_id))
        if self.error is not None:
            raise self.error
        return self.messages.get(conversation_id, ())


class ChatwootReconcilerTests(unittest.TestCase):
    def test_first_run_initializes_cursor_without_historical_scan(self) -> None:
        store = FakeJobStore()
        queue = FakeInboundQueue()
        chatwoot = FakeChatwootReader()
        reconciler = ChatwootReconciler(
            account_id=2,
            store=store,
            queue=queue,
            chatwoot=chatwoot,
        )

        self.assertEqual(reconciler.run_once(now=1_000), 0)
        self.assertEqual(store.get_reconciliation_cursor(), 1_000)
        self.assertEqual(chatwoot.list_calls, [])

    def test_overlap_filters_messages_counts_new_jobs_and_stops_on_old_page(self) -> None:
        store = FakeJobStore(cursor=1_000)
        queue = FakeInboundQueue(duplicate_ids={504})
        chatwoot = FakeChatwootReader()
        chatwoot.pages = {
            1: (
                ChatwootConversation(7, ("vip",), 1_050),
                ChatwootConversation(8, (), 600),
            ),
            2: (ChatwootConversation(9, (), 699),),
        }
        chatwoot.messages[7] = (
            {
                "id": 501,
                "message_type": 0,
                "private": False,
                "content_type": "text",
                "content": "退款多久到帐？",
                "created_at": 701,
                "sender": {"id": 9},
            },
            {
                "id": 502,
                "message_type": 0,
                "private": False,
                "content": "太旧",
                "created_at": 699,
                "sender": {"id": 9},
            },
            {
                "id": 503,
                "message_type": "outgoing",
                "private": False,
                "content": "客服回复",
                "created_at": 702,
                "sender": {"id": 9},
            },
            {
                "id": 504,
                "message_type": "incoming",
                "private": False,
                "content_type": "text",
                "content": "重复消息",
                "created_at": 703,
                "sender": {"id": 10},
            },
            {
                "id": 505,
                "message_type": 0,
                "private": True,
                "content": "私密消息",
                "created_at": 704,
                "sender": {"id": 9},
            },
            {
                "id": 506,
                "message_type": 0,
                "private": False,
                "content_type": "image",
                "content": "图片",
                "created_at": 705,
                "sender": {"id": 9},
            },
        )
        reconciler = ChatwootReconciler(
            account_id=2,
            store=store,
            queue=queue,
            chatwoot=chatwoot,
        )

        count = reconciler.run_once(now=1_060)

        self.assertEqual(count, 1)
        self.assertEqual([item.message_id for item in queue.messages], [501, 504])
        self.assertEqual(queue.messages[0].account_id, 2)
        self.assertEqual(queue.messages[0].labels, ("vip",))
        self.assertEqual(chatwoot.list_calls, [(2, 1), (2, 2)])
        self.assertEqual(chatwoot.message_calls, [(2, 7)])
        self.assertEqual(store.get_reconciliation_cursor(), 1_060)

    def test_chatwoot_failure_preserves_exact_cursor(self) -> None:
        store = FakeJobStore(cursor=1_000)
        chatwoot = FakeChatwootReader()
        chatwoot.error = ChatwootAPIError(503, "remote payload")
        reconciler = ChatwootReconciler(
            account_id=2,
            store=store,
            queue=FakeInboundQueue(),
            chatwoot=chatwoot,
        )

        with self.assertRaises(ChatwootAPIError):
            reconciler.run_once(now=1_060)

        self.assertEqual(store.get_reconciliation_cursor(), 1_000)

    def test_account_id_must_be_an_exact_positive_integer(self) -> None:
        for invalid in (True, 0, -1, "2"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ChatwootReconciler(
                    account_id=invalid,
                    store=FakeJobStore(),
                    queue=FakeInboundQueue(),
                    chatwoot=FakeChatwootReader(),
                )


if __name__ == "__main__":
    unittest.main()
