from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Sequence

from app.dingtalk_adapter import (
    InteractionStatus,
    SQLiteDingTalkState,
)
from app.operations_csv import encode_csv


_HEADERS = (
    "问题编号",
    "提问时间",
    "提问人",
    "会话",
    "渠道",
    "业务板块",
    "原始问题",
    "机器人原始回答",
    "回答动作",
    "原因",
    "用户反馈",
    "处理状态",
    "负责人",
    "最终权威答案",
    "审核人",
    "知识审核状态",
)


def export_interactions_csv(
    *,
    database_path: str | Path,
    output_path: str | Path,
    status: InteractionStatus | None = None,
) -> int:
    state = SQLiteDingTalkState(database_path)
    interactions = state.list_interactions(status=status)
    rows = (
        {
            "问题编号": interaction.ticket_code,
            "提问时间": datetime.fromtimestamp(
                interaction.created_at
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "提问人": interaction.sender_id,
            "会话": interaction.conversation_id,
            "渠道": interaction.channel,
            "业务板块": interaction.domain,
            "原始问题": interaction.question,
            "机器人原始回答": interaction.answer,
            "回答动作": interaction.action,
            "原因": interaction.reason,
            "用户反馈": interaction.feedback,
            "处理状态": interaction.status,
            "负责人": interaction.owner,
            "最终权威答案": interaction.final_answer,
            "审核人": interaction.reviewed_by,
            "知识审核状态": interaction.knowledge_status,
        }
        for interaction in interactions
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encode_csv(_HEADERS, rows))
    return len(interactions)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export DingTalk assistant interactions to a safe CSV file.",
    )
    parser.add_argument(
        "--database",
        default=".runtime/customer-service/dingtalk.db",
        help="DingTalk SQLite state database path.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Destination CSV path.",
    )
    parser.add_argument(
        "--status",
        choices=(
            "awaiting_feedback",
            "awaiting_clarification",
            "needs_human",
            "resolved",
            "closed",
        ),
        default=None,
        help="Optional interaction status filter.",
    )
    args = parser.parse_args(argv)
    count = export_interactions_csv(
        database_path=args.database,
        output_path=args.output,
        status=args.status,
    )
    print(f"Exported {count} interactions to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
