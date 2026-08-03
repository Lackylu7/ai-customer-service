import unittest
from dataclasses import replace

from app.aitable_sync import (
    AITableFieldMap,
    DwsAITableClient,
    interaction_to_cells,
    sync_interactions,
)
from app.dingtalk_adapter import DingTalkInteraction
from app.dingtalk_collaboration_closure import CollaborationDraft


FIELDS = AITableFieldMap(
    ticket_code="field-ticket",
    asked_at="field-time",
    domain="field-domain",
    question="field-question",
    original_answer="field-original-answer",
    bot_action="field-action",
    feedback="field-feedback",
    status="field-status",
    reason="field-reason",
    owner="field-owner",
    final_answer="field-final-answer",
    knowledge_status="field-knowledge-status",
    collaboration_status="field-collaboration-status",
    collaboration_group_name="field-collaboration-group-name",
    root_cause="field-root-cause",
    knowledge_candidate="field-knowledge-candidate",
)


def create_interaction(*, ticket_code: str = "CS-20260728-000001") -> DingTalkInteraction:
    return DingTalkInteraction(
        interaction_id=1,
        ticket_code=ticket_code,
        message_id="private-message-id",
        sender_id="private-staff-id",
        conversation_id="private-conversation-id",
        channel="direct",
        question="现在的制度下，达成多少指标才能开下一家店",
        answer="现有知识不足以安全确认。",
        domain="shareholder",
        action="handoff",
        reason="pending_business_rule",
        feedback="unresolved",
        status="needs_human",
        owner="小草",
        owner_id="private-owner-id",
        final_answer=None,
        reviewed_by=None,
        knowledge_status="not_reviewed",
        collaboration_group_id=None,
        collaboration_group_name=None,
        collaboration_status=None,
        created_at=1785219277,
        updated_at=1785219277,
    )


class RecordingAITableClient:
    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.existing = existing or {}
        self.created: list[dict[str, object]] = []
        self.updated: list[tuple[str, dict[str, object]]] = []

    def list_ticket_records(self, ticket_field_id: str) -> dict[str, str]:
        self.ticket_field_id = ticket_field_id
        return dict(self.existing)

    def create_records(self, records: list[dict[str, object]]) -> None:
        self.created.extend(records)

    def update_records(
        self,
        records: list[tuple[str, dict[str, object]]],
    ) -> None:
        self.updated.extend(records)


class AITableSyncTests(unittest.TestCase):
    def test_maps_only_redacted_operational_fields(self) -> None:
        cells = interaction_to_cells(create_interaction(), FIELDS)

        self.assertEqual(cells["field-domain"], "股东业务")
        self.assertEqual(cells["field-action"], "转负责人")
        self.assertEqual(cells["field-feedback"], "未解决")
        self.assertEqual(cells["field-status"], "待人工")
        self.assertEqual(cells["field-knowledge-status"], "待审核")
        self.assertEqual(cells["field-collaboration-status"], "")
        serialized = repr(cells)
        self.assertNotIn("private-staff-id", serialized)
        self.assertNotIn("private-message-id", serialized)
        self.assertNotIn("private-conversation-id", serialized)
        self.assertNotIn("private-owner-id", serialized)

    def test_maps_collaboration_progress_into_existing_action_field(self) -> None:
        active = replace(
            create_interaction(),
            collaboration_group_id="private-group-id",
            collaboration_group_name="股东业务协作",
            collaboration_status="active",
        )
        resolved = replace(
            active,
            status="resolved",
            collaboration_status="resolved",
        )

        active_cells = interaction_to_cells(active, FIELDS)
        resolved_cells = interaction_to_cells(resolved, FIELDS)

        self.assertEqual(active_cells["field-action"], "协作群处理中")
        self.assertEqual(resolved_cells["field-action"], "协作群已结案")
        self.assertNotIn("private-group-id", repr(active_cells))

    def test_maps_safe_collaboration_summary_fields(self) -> None:
        interaction = create_interaction()
        interaction = DingTalkInteraction(
            **{
                **interaction.__dict__,
                "collaboration_group_id": "private-group-id",
                "collaboration_group_name": "股东业务协作",
                "collaboration_status": "active",
            }
        )
        draft = CollaborationDraft(
            ticket_code=interaction.ticket_code,
            group_id="private-group-id",
            final_answer="负责人核实后的答复。",
            root_cause="原资料没有说明办理入口。",
            knowledge_candidate="办理前应联系股东业务负责人核实。",
            created_at=1_785_000_000,
        )

        cells = interaction_to_cells(interaction, FIELDS, draft)

        self.assertEqual(cells["field-collaboration-status"], "沟通中")
        self.assertEqual(
            cells["field-collaboration-group-name"],
            "股东业务协作",
        )
        self.assertEqual(cells["field-root-cause"], "原资料没有说明办理入口。")
        self.assertEqual(
            cells["field-knowledge-candidate"],
            "办理前应联系股东业务负责人核实。",
        )
        self.assertNotIn("private-group-id", repr(cells))

    def test_creates_missing_rows_and_updates_existing_rows_idempotently(self) -> None:
        interactions = (
            create_interaction(ticket_code="CS-20260728-000001"),
            create_interaction(ticket_code="CS-20260728-000002"),
        )
        client = RecordingAITableClient(
            {"CS-20260728-000002": "record-existing"}
        )

        result = sync_interactions(
            interactions=interactions,
            client=client,
            fields=FIELDS,
        )

        self.assertEqual(result.total, 2)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.updated, 1)
        self.assertEqual(client.ticket_field_id, "field-ticket")
        self.assertEqual(
            client.created[0]["field-ticket"],
            "CS-20260728-000001",
        )
        self.assertEqual(client.updated[0][0], "record-existing")
        self.assertEqual(
            client.updated[0][1]["field-ticket"],
            "CS-20260728-000002",
        )

    def test_dws_client_reads_existing_tickets_and_uses_record_files(self) -> None:
        calls: list[tuple[tuple[str, ...], object | None]] = []

        def runner(args: tuple[str, ...]) -> dict[str, object]:
            imported = None
            if "--records-file" in args:
                import json
                from pathlib import Path

                path = Path(args[args.index("--records-file") + 1])
                imported = json.loads(path.read_text(encoding="utf-8"))
            calls.append((args, imported))
            if "query" in args:
                return {
                    "data": {
                        "records": [
                            {
                                "recordId": "record-1",
                                "cells": {"field-ticket": "CS-1"},
                            }
                        ]
                    }
                }
            return {"data": {}, "success": True}

        client = DwsAITableClient(
            base_id="base-1",
            table_id="table-1",
            runner=runner,
        )

        existing = client.list_ticket_records("field-ticket")
        client.create_records([{"field-ticket": "CS-2"}])
        client.update_records(
            [("record-1", {"field-ticket": "CS-1", "status": "已解决"})]
        )

        self.assertEqual(existing, {"CS-1": "record-1"})
        self.assertEqual(calls[1][1], [{"cells": {"field-ticket": "CS-2"}}])
        self.assertEqual(
            calls[2][1],
            [
                {
                    "recordId": "record-1",
                    "cells": {"field-ticket": "CS-1", "status": "已解决"},
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
