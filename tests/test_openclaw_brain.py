import json
import subprocess
import unittest

from app.dify_chat_client import DifyChatResponse
from app.openclaw_brain import (
    OpenClawBusinessDialoguePlanner,
    OpenClawGeneralConversation,
    OpenClawShareholderDialoguePlanner,
    OpenClawShareholderBrain,
    build_openclaw_business_dialogue_planner,
    build_openclaw_general_conversation,
    build_openclaw_shareholder_brain,
    build_openclaw_shareholder_dialogue_planner,
)


class EvidenceBrain:
    def __init__(
        self,
        *,
        answer: str = "每一笔入股都需要提交 OA。",
        resources: tuple[dict[str, object], ...] = (
            {"document_name": "enterprise-knowledge-shareholder.md"},
        ),
    ) -> None:
        self.answer = answer
        self.resources = resources
        self.calls = []

    def send_message(self, *, query, user, conversation_id=None):
        self.calls.append((query, user, conversation_id))
        return DifyChatResponse(
            answer=self.answer,
            conversation_id="dify-conversation-1",
            retriever_resources=self.resources,
        )


class RecordingRunner:
    def __init__(self, reply: str, *, returncode: int = 0) -> None:
        self.reply = reply
        self.returncode = returncode
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout=json.dumps(
                {
                    "payloads": [
                        {"text": self.reply, "mediaUrl": None},
                    ],
                    "meta": {"durationMs": 1200},
                },
                ensure_ascii=False,
            ),
            stderr="",
        )


class OpenClawShareholderBrainTests(unittest.TestCase):
    def test_uses_dify_evidence_then_calls_isolated_agent_without_delivery(self) -> None:
        evidence = EvidenceBrain()
        runner = RecordingRunner("需要，每一笔入股都必须通过 OA 提交。")
        brain = OpenClawShareholderBrain(
            evidence_brain=evidence,
            runner=runner,
        )

        result = brain.send_message(
            query="申请入股需要走 OA 吗？",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.answer, "需要，每一笔入股都必须通过 OA 提交。")
        self.assertEqual(result.conversation_id, "dify-conversation-1")
        command = runner.calls[0][0]
        self.assertEqual(command[:4], ["openclaw", "agent", "--agent", "shareholder-3-0"])
        self.assertIn("--json", command)
        self.assertNotIn("--deliver", command)
        session_id = command[command.index("--session-id") + 1]
        self.assertTrue(session_id.startswith("shareholder-"))
        self.assertNotIn("user-1", session_id)

    def test_does_not_call_openclaw_without_approved_shareholder_source(self) -> None:
        runner = RecordingRunner("不应被调用")
        brain = OpenClawShareholderBrain(
            evidence_brain=EvidenceBrain(
                resources=({"document_name": "enterprise-knowledge-finance.md"},)
            ),
            runner=runner,
        )

        result = brain.send_message(
            query="申请入股需要走 OA 吗？",
            user="dingtalk:user-1",
        )

        self.assertTrue(result.answer.startswith("[HANDOFF]"))
        self.assertEqual(result.retriever_resources, ())
        self.assertEqual(runner.calls, [])

    def test_dify_handoff_bypasses_openclaw(self) -> None:
        runner = RecordingRunner("不应被调用")
        brain = OpenClawShareholderBrain(
            evidence_brain=EvidenceBrain(
                answer="[HANDOFF]现有资料无法确认，请人工核实。"
            ),
            runner=runner,
        )

        result = brain.send_message(
            query="尚未确认的规则是什么？",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.answer, "[HANDOFF]现有资料无法确认，请人工核实。")
        self.assertEqual(runner.calls, [])

    def test_new_numeric_claim_falls_back_to_evidence_answer(self) -> None:
        runner = RecordingRunner("需要在 7 天内提交 OA。")
        brain = OpenClawShareholderBrain(
            evidence_brain=EvidenceBrain(),
            runner=runner,
        )

        result = brain.send_message(
            query="申请入股需要走 OA 吗？",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.answer, "每一笔入股都需要提交 OA。")

    def test_nonzero_cli_exit_falls_back_to_evidence_answer(self) -> None:
        runner = RecordingRunner("", returncode=1)
        brain = OpenClawShareholderBrain(
            evidence_brain=EvidenceBrain(),
            runner=runner,
        )

        result = brain.send_message(
            query="申请入股需要走 OA 吗？",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.answer, "每一笔入股都需要提交 OA。")

    def test_env_builder_is_off_by_default_and_validates_toggle(self) -> None:
        evidence = EvidenceBrain()
        self.assertIsNone(
            build_openclaw_shareholder_brain(
                evidence_brain=evidence,
                environ={},
            )
        )
        with self.assertRaisesRegex(ValueError, "true or false"):
            build_openclaw_shareholder_brain(
                evidence_brain=evidence,
                environ={"OPENCLAW_SHAREHOLDER_ENABLED": "sometimes"},
            )


class OpenClawShareholderDialoguePlannerTests(unittest.TestCase):
    def test_asks_openclaw_for_one_missing_context_question(self) -> None:
        runner = RecordingRunner(
            json.dumps(
                {
                    "action": "clarify",
                    "question": "想先确认一下，你目前是族长，还是已经入股的股东呢？",
                },
                ensure_ascii=False,
            )
        )
        planner = OpenClawShareholderDialoguePlanner(runner=runner)

        result = planner.plan(
            question="我想开下一家店，需要达到哪些条件？",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "clarify")
        self.assertIn("族长", result.question)
        command = runner.calls[0][0]
        self.assertIn("--json", command)
        self.assertNotIn("--deliver", command)
        self.assertNotIn("user-1", command[command.index("--session-id") + 1])

    def test_clear_question_can_continue_without_clarification(self) -> None:
        runner = RecordingRunner(
            json.dumps(
                {"action": "continue", "question": ""},
                ensure_ascii=False,
            )
        )
        planner = OpenClawShareholderDialoguePlanner(runner=runner)

        result = planner.plan(
            question="公司目前有哪些股东类型？",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "continue")
        self.assertEqual(len(runner.calls), 1)

    def test_global_planner_clarifies_a_non_store_ambiguous_question(self) -> None:
        runner = RecordingRunner(
            json.dumps(
                {
                    "action": "clarify",
                    "question": "想确认一下，你说的是哪家门店、哪个月份的分红呢？",
                },
                ensure_ascii=False,
            )
        )
        planner = OpenClawShareholderDialoguePlanner(runner=runner)

        result = planner.plan(
            question="我的分红好像不对。",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "clarify")
        self.assertIn("哪家门店", result.question)

    def test_invalid_openclaw_reply_uses_safe_local_clarification(self) -> None:
        planner = OpenClawShareholderDialoguePlanner(
            runner=RecordingRunner("我觉得你可以直接开店。")
        )

        result = planner.plan(
            question="我想开下一家店，需要达到哪些条件？",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "clarify")
        self.assertIn("族长", result.question)

    def test_openclaw_cannot_skip_a_required_role_clarification(self) -> None:
        runner = RecordingRunner(
            json.dumps(
                {"action": "continue", "question": ""},
                ensure_ascii=False,
            )
        )
        planner = OpenClawShareholderDialoguePlanner(runner=runner)

        result = planner.plan(
            question="我想开下一家店，需要达到哪些条件？",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "clarify")
        self.assertIn("族长", result.question)

    def test_dialogue_builder_uses_the_same_feature_toggle(self) -> None:
        self.assertIsNone(
            build_openclaw_shareholder_dialogue_planner(environ={})
        )
        planner = build_openclaw_shareholder_dialogue_planner(
            environ={"OPENCLAW_SHAREHOLDER_ENABLED": "true"}
        )
        self.assertIsInstance(planner, OpenClawShareholderDialoguePlanner)


class OpenClawBusinessDialoguePlannerTests(unittest.TestCase):
    def test_clarifies_an_ambiguous_finance_question(self) -> None:
        runner = RecordingRunner(
            json.dumps(
                {
                    "action": "clarify",
                    "question": "想确认一下，你说的是哪个月份、哪一项薪酬呢？",
                },
                ensure_ascii=False,
            )
        )
        planner = OpenClawBusinessDialoguePlanner(runner=runner)

        result = planner.plan(
            question="我的工资好像不对。",
            domain="finance",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "clarify")
        self.assertIn("哪个月份", result.question)
        command = runner.calls[0][0]
        prompt = command[command.index("--message") + 1]
        self.assertIn("财务与薪酬", prompt)
        self.assertNotIn("--deliver", command)

    def test_invalid_reply_continues_to_safe_retrieval(self) -> None:
        planner = OpenClawBusinessDialoguePlanner(
            runner=RecordingRunner("直接给员工一个答案。")
        )

        result = planner.plan(
            question="报销好像不对。",
            domain="finance",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "continue")

    def test_builder_inherits_general_conversation_toggle(self) -> None:
        self.assertIsNone(build_openclaw_business_dialogue_planner(environ={}))
        enabled = build_openclaw_business_dialogue_planner(
            environ={"OPENCLAW_GENERAL_CONVERSATION_ENABLED": "true"}
        )
        self.assertIsInstance(enabled, OpenClawBusinessDialoguePlanner)


class OpenClawGeneralConversationTests(unittest.TestCase):
    def test_answers_normal_language_without_delivery(self) -> None:
        runner = RecordingRunner(
            json.dumps(
                {
                    "action": "answer",
                    "text": "我就在这里呀，现在这个助手已经接入龙虾了。",
                },
                ensure_ascii=False,
            )
        )
        conversation = OpenClawGeneralConversation(runner=runner)

        result = conversation.respond(
            message="龙虾在哪",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "allow")
        self.assertIn("就在这里", result.text)
        command = runner.calls[0][0]
        self.assertIn("internal-companion", command)
        self.assertNotIn("--deliver", command)

    def test_can_ask_one_question_for_ambiguous_business_language(self) -> None:
        runner = RecordingRunner(
            json.dumps(
                {
                    "action": "clarify",
                    "text": "你说的是哪家门店的哪项业务呢？",
                },
                ensure_ascii=False,
            )
        )
        conversation = OpenClawGeneralConversation(runner=runner)

        result = conversation.respond(
            message="这个好像不对",
            user="dingtalk:user-1",
        )

        self.assertEqual(result.action, "clarify")
        self.assertIn("哪家门店", result.text)

    def test_builder_has_an_independent_feature_toggle(self) -> None:
        self.assertIsNone(build_openclaw_general_conversation(environ={}))
        enabled = build_openclaw_general_conversation(
            environ={"OPENCLAW_GENERAL_CONVERSATION_ENABLED": "true"}
        )
        self.assertIsInstance(enabled, OpenClawGeneralConversation)


if __name__ == "__main__":
    unittest.main()
