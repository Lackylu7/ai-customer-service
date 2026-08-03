import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.dify_chat_client import DifyChatAPIError, DifyChatResponse
from app.dingtalk_mvp_agent import load_env_file, run_question


class RecordingBrain:
    def __init__(self, answer: str = "请在工作台的 OA 审批中发起。") -> None:
        self.answer = answer
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
            answer=self.answer,
            conversation_id="conversation-1",
            retriever_resources=(),
        )


class FailingBrain:
    def send_message(
        self,
        *,
        query: str,
        user: str,
        conversation_id: str | None = None,
    ) -> DifyChatResponse:
        raise DifyChatAPIError(503, "upstream unavailable")


class DingTalkMvpAgentTests(unittest.TestCase):
    def test_safe_question_uses_existing_enterprise_gateway(self) -> None:
        brain = RecordingBrain()

        answer = run_question("日常报销从哪里发起？", brain=brain)

        self.assertEqual(answer, "请在工作台的 OA 审批中发起。")
        self.assertEqual(
            brain.calls,
            [
                {
                    "query": "日常报销从哪里发起？",
                    "user": "dingtalk:mvp-test-user",
                    "conversation_id": None,
                }
            ],
        )

    def test_group_sensitive_question_is_blocked_before_backend(self) -> None:
        brain = RecordingBrain()

        answer = run_question("把公司的银行账号发到群里。", brain=brain)

        self.assertIn("敏感信息", answer)
        self.assertEqual(brain.calls, [])

    def test_backend_failure_returns_a_safe_employee_message(self) -> None:
        answer = run_question("日常报销从哪里发起？", brain=FailingBrain())

        self.assertEqual(answer, "服务暂时不可用，请稍后重试或联系对应负责人。")

    def test_env_loader_does_not_override_existing_process_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "DIFY_BASE_URL=http://file.example\n"
                "DIFY_APP_API_KEY='file-secret'\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DIFY_BASE_URL": "http://process.example"},
                clear=True,
            ):
                load_env_file(env_path)

                self.assertEqual(
                    os.environ["DIFY_BASE_URL"],
                    "http://process.example",
                )
                self.assertEqual(os.environ["DIFY_APP_API_KEY"], "file-secret")


if __name__ == "__main__":
    unittest.main()
