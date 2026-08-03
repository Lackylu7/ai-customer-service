import unittest

from app.handoff_policy import (
    handoff_reason,
    has_handoff_sentinel,
    parse_dify_answer,
)


class HandoffPolicyTests(unittest.TestCase):
    def test_explicit_human_request_requires_handoff(self) -> None:
        self.assertEqual(
            handoff_reason("别让机器人回了，我要人工客服"),
            "explicit_human_request",
        )

    def test_complaint_or_legal_issue_requires_handoff(self) -> None:
        self.assertEqual(
            handoff_reason("再不解决我就向消协投诉"),
            "complaint_or_legal",
        )

    def test_payment_and_security_disputes_require_handoff(self) -> None:
        self.assertEqual(
            handoff_reason("银行卡被重复扣款了"),
            "payment_or_account_security",
        )
        self.assertEqual(
            handoff_reason("我的账号可能被盗了"),
            "payment_or_account_security",
        )

    def test_normal_faq_stays_with_ai(self) -> None:
        self.assertIsNone(handoff_reason("现货下单后多久发货？"))
        self.assertIsNone(handoff_reason("支持银行卡付款吗？"))

    def test_dify_handoff_sentinel_is_not_shown_to_customer(self) -> None:
        parsed = parse_dify_answer("[HANDOFF]当前资料无法确认，请转人工核实。")

        self.assertTrue(parsed.needs_human)
        self.assertEqual(parsed.customer_text, "当前资料无法确认，请转人工核实。")

    def test_embedded_sentinel_is_removed_and_marks_handoff(self) -> None:
        parsed = parse_dify_answer(
            "抱歉，当前资料没有说明。 [HANDOFF]正在为你转接人工客服，请稍候。"
        )

        self.assertTrue(parsed.needs_human)
        self.assertEqual(parsed.customer_text, "当前资料无法确认，请转人工核实。")

    def test_text_before_embedded_sentinel_is_not_sent_to_customer(self) -> None:
        parsed = parse_dify_answer(
            "您好，现有知识库中没有相关流程。建议先联系人事。\n\n"
            "[HANDOFF]现有知识不足以安全确认该问题，需要联系对应负责人核实。"
        )

        self.assertTrue(parsed.needs_human)
        self.assertEqual(
            parsed.customer_text,
            "现有知识不足以安全确认该问题，需要联系对应负责人核实。",
        )

    def test_handoff_sentinel_detection_is_case_insensitive(self) -> None:
        self.assertTrue(has_handoff_sentinel("说明文字 [handoff]请人工核实"))

    def test_unverified_handoff_operation_is_replaced(self) -> None:
        parsed = parse_dify_answer("正在为你转接人工客服，请稍候。")

        self.assertTrue(parsed.needs_human)
        self.assertEqual(parsed.customer_text, "当前资料无法确认，请转人工核实。")

    def test_plain_language_knowledge_gap_is_replaced(self) -> None:
        parsed = parse_dify_answer("抱歉，根据现有知识库，我无法回答这个问题。")

        self.assertTrue(parsed.needs_human)
        self.assertEqual(parsed.customer_text, "当前资料无法确认，请转人工核实。")

    def test_normal_dify_answer_is_preserved(self) -> None:
        parsed = parse_dify_answer("  现货会在 48 小时内发出。  ")

        self.assertFalse(parsed.needs_human)
        self.assertEqual(parsed.customer_text, "现货会在 48 小时内发出。")

    def test_empty_dify_answer_requires_handoff(self) -> None:
        parsed = parse_dify_answer("   ")

        self.assertTrue(parsed.needs_human)
        self.assertEqual(parsed.customer_text, "当前资料无法确认，请转人工核实。")


if __name__ == "__main__":
    unittest.main()
