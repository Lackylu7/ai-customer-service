import unittest

from app.assistant_brain import ConfiguredAssistantBrain
from app.dify_chat_client import DifyChatResponse


class RecordingBrain:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, str | None]] = []

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
        return DifyChatResponse(
            answer=f"{self.name} answer",
            conversation_id=f"{self.name}-conversation",
            retriever_resources=(),
        )


class ConfiguredAssistantBrainTests(unittest.TestCase):
    def test_routes_all_requests_to_the_configured_backend(self) -> None:
        dragon = RecordingBrain("dragon")
        dify = RecordingBrain("dify")
        brain = ConfiguredAssistantBrain(
            active_backend="dragon",
            backends={"dragon": dragon, "dify": dify},
        )

        result = brain.send_message(
            query="新教练怎么入职？",
            user="dingtalk:user-1",
            conversation_id="conversation-0",
        )

        self.assertEqual(brain.active_backend, "dragon")
        self.assertEqual(result.answer, "dragon answer")
        self.assertEqual(
            dragon.calls,
            [
                {
                    "query": "新教练怎么入职？",
                    "user": "dingtalk:user-1",
                    "conversation_id": "conversation-0",
                }
            ],
        )
        self.assertEqual(dify.calls, [])

    def test_rejects_an_unconfigured_active_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "dragon"):
            ConfiguredAssistantBrain(
                active_backend="dragon",
                backends={"dify": RecordingBrain("dify")},
            )


if __name__ == "__main__":
    unittest.main()
