import base64
import binascii
import logging
import os
import sqlite3
import time
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.chatwoot_client import ChatwootClient
from app.chatwoot_webhook import (
    WebhookAuthenticationError,
    WebhookPayloadError,
    process_chatwoot_webhook,
)
from app.conversation_store import SQLiteConversationStore
from app.customer_service import CustomerService
from app.dify_chat_client import DifyChatClient
from app.dify_client import DifyAPIError, DifyClient
from app.document_parser import DocumentParsingError, parse_document
from app.knowledge_ingestion import FileTooLargeError, KnowledgeIngestionService
from app.inbound_queue import InboundQueue
from app.job_store import SQLiteJobStore
from app.operations_api import operations_router
from app.operations_models import NullOperationsRecorder, OperationsRecorder
from app.operations_store import OperationsStore
from app.settings import Settings


app = FastAPI(
    title="AI Customer Service Document API",
    version="0.1.0",
)
app.include_router(operations_router)
logger = logging.getLogger(__name__)
MAX_UPLOAD_FILE_SIZE = 20 * 1024 * 1024
OPERATIONS_PAGE = Path(__file__).parent / "static" / "operations.html"


class FileSource(BaseModel):
    base64_string: str
    filename: str = Field(min_length=1)


class ConversionOptions(BaseModel):
    from_formats: list[str] = Field(default_factory=list)
    to_formats: list[str] = Field(default_factory=lambda: ["md"])
    do_ocr: bool = True


class ConversionRequest(BaseModel):
    file_sources: list[FileSource] = Field(min_length=1, max_length=1)
    options: ConversionOptions = Field(default_factory=ConversionOptions)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/operations", include_in_schema=False)
def operations_page() -> FileResponse:
    return FileResponse(
        OPERATIONS_PAGE,
        media_type="text/html; charset=utf-8",
    )


@lru_cache(maxsize=1)
def get_customer_service(settings: Settings) -> CustomerService:
    return CustomerService(
        store=SQLiteConversationStore(settings.customer_service_db_path),
        dify_client=DifyChatClient(
            base_url=settings.dify_base_url,
            api_key=settings.dify_app_api_key,
            timeout=60,
        ),
        chatwoot_client=ChatwootClient(
            base_url=settings.chatwoot_base_url,
            api_token=settings.chatwoot_api_token,
            timeout=15,
        ),
        operations_recorder=get_operations_recorder(
            settings.customer_service_db_path
        ),
    )


def get_operations_recorder(database_path: Path) -> OperationsRecorder:
    try:
        return OperationsStore(database_path)
    except Exception:
        logger.warning(
            "Operations store initialization failed; metrics are disabled.",
            exc_info=True,
        )
        return NullOperationsRecorder()


@lru_cache(maxsize=8)
def get_inbound_queue(database_path: Path) -> InboundQueue:
    return InboundQueue(
        SQLiteJobStore(database_path),
        get_operations_recorder(database_path),
    )


@app.post("/v1/chatwoot/webhook")
async def chatwoot_webhook(request: Request, response: Response) -> dict[str, str]:
    raw_body = await request.body()
    if len(raw_body) > 256 * 1024:
        raise HTTPException(status_code=413, detail="Webhook payload is too large.")

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI customer service is not configured.",
        ) from exc

    try:
        result = process_chatwoot_webhook(
            raw_body=raw_body,
            signature=request.headers.get("X-Chatwoot-Signature"),
            timestamp=request.headers.get("X-Chatwoot-Timestamp"),
            enqueuer=get_inbound_queue(settings.customer_service_db_path),
            webhook_secret=settings.chatwoot_webhook_secret,
            expected_account_id=settings.chatwoot_account_id,
        )
    except WebhookAuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Invalid webhook signature.") from exc
    except WebhookPayloadError as exc:
        raise HTTPException(status_code=422, detail="Invalid webhook payload.") from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503,
            detail="AI customer service is temporarily unavailable.",
        ) from exc

    if result.status == "queued":
        response.status_code = 202

    return {"status": result.status}


@app.post("/v1/convert/source")
def convert_source(request: ConversionRequest) -> dict[str, object]:
    source = request.file_sources[0]

    try:
        content = base64.b64decode(source.base64_string, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid base64 document content.") from exc

    started_at = time.perf_counter()

    try:
        markdown = parse_document(content, source.filename)
    except DocumentParsingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "document": {
            "md_content": markdown,
            "json_content": {},
            "html_content": "",
            "text_content": "",
            "doctags_content": "",
        },
        "status": "success",
        "processing_time": time.perf_counter() - started_at,
        "timings": {},
        "errors": [],
    }


@app.post("/v1/knowledge/documents", status_code=202)
def ingest_knowledge_document(file: UploadFile = File(...)) -> dict[str, object]:
    dify_base_url = os.getenv("DIFY_BASE_URL", "http://127.0.0.1:8081")
    dataset_id = os.getenv("DIFY_DATASET_ID", "").strip()
    api_key = os.getenv("DIFY_DATASET_API_KEY", "").strip()
    indexing_technique = os.getenv("DIFY_INDEXING_TECHNIQUE", "economy").strip()
    if not dataset_id or not api_key:
        raise HTTPException(status_code=503, detail="Dify knowledge ingestion is not configured.")

    service = KnowledgeIngestionService(
        dataset_id=dataset_id,
        dify_client=DifyClient(base_url=dify_base_url, api_key=api_key, timeout=60),
        parser=parse_document,
        max_file_size=MAX_UPLOAD_FILE_SIZE,
        indexing_technique=indexing_technique,
    )
    content = file.file.read(MAX_UPLOAD_FILE_SIZE + 1)

    try:
        result = service.ingest(content, file.filename or "")
    except FileTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except DocumentParsingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DifyAPIError as exc:
        raise HTTPException(status_code=502, detail="Dify knowledge ingestion failed.") from exc

    return asdict(result)
