from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Sequence

from app.aitable_sync import (
    AITableFieldMap,
    DwsAITableClient,
    DwsAITableError,
    subprocess_dws_runner,
    sync_interactions,
)
from app.dingtalk_adapter import DingTalkInteraction, SQLiteDingTalkState
from app.dingtalk_collaboration_closure import (
    CollaborationDraft,
    SQLiteCollaborationTranscriptStore,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync privacy-safe DingTalk interactions to an AI Table.",
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--base-id", required=True)
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--field-map", required=True)
    parser.add_argument("--updated-after", type=int, default=0)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument(
        "--dws-command",
        default=shutil.which("dws.cmd") or shutil.which("dws") or "dws",
    )
    args = parser.parse_args(argv)
    if args.interval < 2:
        parser.error("--interval must be at least 2 seconds")

    field_payload = json.loads(
        Path(args.field_map).read_text(encoding="utf-8")
    )
    fields = AITableFieldMap(**field_payload)
    state = SQLiteDingTalkState(args.database)
    transcripts = SQLiteCollaborationTranscriptStore(args.database)
    client = DwsAITableClient(
        base_id=args.base_id,
        table_id=args.table_id,
        runner=subprocess_dws_runner((args.dws_command,)),
    )

    if not args.watch:
        interactions = _updated_interactions(
            state.list_interactions(),
            updated_after=args.updated_after,
        )
        _sync_and_print(
            interactions,
            client=client,
            fields=fields,
            transcripts=transcripts,
        )
        return 0

    seen_versions: dict[str, int] = {}
    while True:
        interactions = tuple(
            interaction
            for interaction in _updated_interactions(
                state.list_interactions(),
                updated_after=args.updated_after,
            )
            if seen_versions.get(interaction.ticket_code)
            != interaction.updated_at
        )
        if interactions:
            try:
                _sync_and_print(
                    interactions,
                    client=client,
                    fields=fields,
                    transcripts=transcripts,
                )
            except DwsAITableError:
                print(
                    "AI Table sync failed; the next poll will retry.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                seen_versions.update(
                    {
                        interaction.ticket_code: interaction.updated_at
                        for interaction in interactions
                    }
                )
        time.sleep(args.interval)


def _updated_interactions(
    interactions: Sequence[DingTalkInteraction],
    *,
    updated_after: int,
) -> tuple[DingTalkInteraction, ...]:
    return tuple(
        interaction
        for interaction in interactions
        if interaction.updated_at >= updated_after
    )


def _sync_and_print(
    interactions: Sequence[DingTalkInteraction],
    *,
    client: DwsAITableClient,
    fields: AITableFieldMap,
    transcripts: SQLiteCollaborationTranscriptStore,
) -> None:
    if not interactions:
        result_payload = {"success": True, "total": 0, "created": 0, "updated": 0}
    else:
        result = sync_interactions(
            interactions=interactions,
            client=client,
            fields=fields,
            drafts_by_ticket=_collaboration_drafts(
                interactions,
                transcripts=transcripts,
            ),
        )
        result_payload = {
            "success": True,
            "total": result.total,
            "created": result.created,
            "updated": result.updated,
        }
    print(json.dumps(result_payload, ensure_ascii=False), flush=True)


def _collaboration_drafts(
    interactions: Sequence[DingTalkInteraction],
    *,
    transcripts: SQLiteCollaborationTranscriptStore,
) -> dict[str, CollaborationDraft]:
    drafts: dict[str, CollaborationDraft] = {}
    for interaction in interactions:
        if interaction.collaboration_group_id is None:
            continue
        draft = transcripts.get_draft(
            group_id=interaction.collaboration_group_id
        )
        if draft is not None and draft.ticket_code == interaction.ticket_code:
            drafts[interaction.ticket_code] = draft
    return drafts


if __name__ == "__main__":
    raise SystemExit(main())
