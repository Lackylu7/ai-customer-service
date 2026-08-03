import csv
import tempfile
import unittest
from pathlib import Path

from app.dingtalk_adapter import (
    DingTalkInboundMessage,
    SQLiteDingTalkState,
)
from app.enterprise_assistant import EnterpriseAnswer
from app.interaction_export import export_interactions_csv


class InteractionExportTests(unittest.TestCase):
    def test_exports_only_human_review_items_with_auditable_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = SQLiteDingTalkState(root / "dingtalk.db")
            state.record_interaction(
                message=DingTalkInboundMessage(
                    message_id="message-1",
                    sender_id="staff-1",
                    conversation_id="conversation-1",
                    text="风险金可以提前提现吗？",
                    channel="direct",
                ),
                answer=EnterpriseAnswer(
                    domain="shareholder",
                    action="allow",
                    text="风险金不可以提前提取。",
                ),
            )
            handoff = state.record_interaction(
                message=DingTalkInboundMessage(
                    message_id="message-2",
                    sender_id="staff-2",
                    conversation_id="conversation-2",
                    text="我这个月能分多少钱？",
                    channel="direct",
                ),
                answer=EnterpriseAnswer(
                    domain="shareholder",
                    action="handoff",
                    text="需要财务核实个人数据。",
                    reason="sensitive_financial_data",
                ),
            )
            output_path = root / "needs-human.csv"

            exported = export_interactions_csv(
                database_path=root / "dingtalk.db",
                output_path=output_path,
                status="needs_human",
            )

            self.assertEqual(exported, 1)
            with output_path.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["问题编号"], handoff.ticket_code)
            self.assertEqual(rows[0]["原始问题"], "我这个月能分多少钱？")
            self.assertEqual(rows[0]["机器人原始回答"], "需要财务核实个人数据。")
            self.assertEqual(rows[0]["处理状态"], "needs_human")
            self.assertEqual(rows[0]["原因"], "sensitive_financial_data")
            self.assertRegex(
                rows[0]["提问时间"],
                r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
            )
            self.assertIn("负责人", rows[0])
            self.assertIn("最终权威答案", rows[0])
            self.assertIn("知识审核状态", rows[0])


if __name__ == "__main__":
    unittest.main()
