import hmac
import time
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.operations_csv import encode_csv
from app.job_store import SQLiteJobStore
from app.operations_models import GapStatus
from app.operations_store import OperationsStore, Window
from app.settings import OperationsSettings


GapFilter = Literal["pending", "resolved", "ignored", "all"]
ExportKind = Literal["events", "gaps"]

EVENT_HEADERS = (
    "id",
    "message_id",
    "conversation_id",
    "outcome",
    "reason",
    "duration_ms",
    "created_at",
)
GAP_HEADERS = (
    "id",
    "sample_question",
    "occurrence_count",
    "first_seen_at",
    "last_seen_at",
    "last_conversation_id",
    "status",
    "note",
    "updated_at",
)

operations_router = APIRouter(prefix="/v1/operations", tags=["operations"])


class GapUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: GapStatus
    note: str = Field(max_length=1000)


def current_timestamp() -> int:
    return int(time.time())


def require_operations_settings(request: Request) -> OperationsSettings:
    try:
        settings = OperationsSettings.from_env()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="Operations API is not configured.",
        ) from exc
    if not settings.api_key:
        raise HTTPException(
            status_code=503,
            detail="Operations API is not configured.",
        )
    provided = request.headers.get("X-Operations-Key", "")
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8"),
        settings.api_key.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="Invalid operations key.")
    return settings


OperationsDependency = Annotated[
    OperationsSettings,
    Depends(require_operations_settings),
]


@operations_router.get("/summary")
def operations_summary(
    settings: OperationsDependency,
    window: Annotated[Window, Query()] = "7d",
) -> dict[str, object]:
    return OperationsStore(settings.database_path).get_summary(window)


@operations_router.get("/queue")
def operations_queue(settings: OperationsDependency) -> dict[str, object]:
    store = SQLiteJobStore(settings.database_path)
    metrics = store.metrics(now=current_timestamp())
    dependencies = store.dependency_statuses()
    for name in ("dify", "chatwoot"):
        dependencies.setdefault(
            name,
            {"ok": False, "error_code": "not_checked", "checked_at": 0},
        )
    return {
        "queue": asdict(metrics),
        "dependencies": dependencies,
    }


@operations_router.get("/gaps")
def operations_gaps(
    settings: OperationsDependency,
    status: Annotated[GapFilter, Query()] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    items, total = OperationsStore(settings.database_path).list_gaps(
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [asdict(item) for item in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@operations_router.patch("/gaps/{gap_id}")
def update_operations_gap(
    update: GapUpdate,
    settings: OperationsDependency,
    gap_id: Annotated[int, Path(gt=0)],
) -> dict[str, object]:
    gap = OperationsStore(settings.database_path).update_gap(
        gap_id,
        status=update.status,
        note=update.note,
    )
    if gap is None:
        raise HTTPException(status_code=404, detail="Knowledge gap not found.")
    return asdict(gap)


@operations_router.get("/export.csv")
def export_operations_csv(
    settings: OperationsDependency,
    window: Annotated[Window, Query()] = "7d",
    kind: Annotated[ExportKind, Query()] = "events",
) -> Response:
    store = OperationsStore(settings.database_path)
    if kind == "events":
        headers = EVENT_HEADERS
        rows = store.export_events(window)
    else:
        headers = GAP_HEADERS
        rows = store.export_gaps(window)
    return Response(
        content=encode_csv(headers, rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="operations-{kind}-{window}.csv"'
            )
        },
    )
