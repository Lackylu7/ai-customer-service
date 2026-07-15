import unittest

from app.job_models import PreparedAction


class PreparedActionTests(unittest.TestCase):
    def test_reply_requires_text_and_ai_outcome(self) -> None:
        action = PreparedAction.reply("48 小时内发货。", "dify-1")

        self.assertEqual(action.decision, "reply")
        self.assertEqual(action.outcome, "replied")
        self.assertEqual(action.customer_text, "48 小时内发货。")
        self.assertEqual(action.dify_conversation_id, "dify-1")

    def test_handoff_and_ignore_factories_have_stable_fields(self) -> None:
        handoff = PreparedAction.handoff("正在转接人工。", "knowledge_gap")
        ignored = PreparedAction.ignore_human_mode()

        self.assertEqual((handoff.decision, handoff.outcome), ("handoff", "handed_off"))
        self.assertTrue(handoff.record_knowledge_gap)
        self.assertEqual((ignored.decision, ignored.outcome), ("ignore", "ignored_human_mode"))
        self.assertIsNone(ignored.customer_text)


if __name__ == "__main__":
    unittest.main()
