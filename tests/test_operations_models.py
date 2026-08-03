import unittest

from app.operations_models import RecoveringOperationsRecorder


class FakeOperationsRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.gaps: list[dict[str, object]] = []

    def record_event(self, **values: object) -> None:
        self.events.append(values)

    def record_knowledge_gap(self, **values: object) -> None:
        self.gaps.append(values)


class RecoveringOperationsRecorderTests(unittest.TestCase):
    def test_failed_initialization_is_retried_and_success_is_cached(self) -> None:
        delegate = FakeOperationsRecorder()
        attempts = 0

        def factory() -> FakeOperationsRecorder:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database temporarily unavailable")
            return delegate

        recorder = RecoveringOperationsRecorder(factory)

        with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
            recorder.record_event(
                message_id=1,
                conversation_id=2,
                outcome="duplicate",
                reason=None,
                duration_ms=0,
            )

        recorder.record_event(
            message_id=1,
            conversation_id=2,
            outcome="duplicate",
            reason=None,
            duration_ms=0,
        )
        recorder.record_knowledge_gap(
            question="unknown policy",
            conversation_id=2,
        )

        self.assertEqual(attempts, 2)
        self.assertEqual(len(delegate.events), 1)
        self.assertEqual(len(delegate.gaps), 1)


if __name__ == "__main__":
    unittest.main()
