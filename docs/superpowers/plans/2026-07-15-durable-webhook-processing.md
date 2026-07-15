# Durable Webhook Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace synchronous Chatwoot webhook processing with a durable SQLite queue and recoverable Worker pipeline for a single-server, single-enterprise gray release.

**Architecture:** The FastAPI webhook validates and persists normalized incoming messages, then returns within one second. A two-thread Worker claims jobs atomically, persists the generated action before Chatwoot delivery, preserves per-conversation order, retries recoverable failures, and reconciles missed messages from Chatwoot.

**Tech Stack:** Python 3.12, FastAPI, SQLite WAL, standard-library threads and HTTP clients, Chatwoot, Dify, DeepSeek, PowerShell, Docker Compose, `unittest`.

## Global Constraints

- Target one server and one enterprise; do not add multi-node coordination.
- Add no production dependency in this plan; use Python and SQLite standard-library capabilities.
- Default Worker concurrency is exactly `2`.
- Default job lease is exactly `300` seconds.
- Default reconciliation interval is exactly `60` seconds.
- Generation attempts are capped at `2` total attempts; Chatwoot delivery attempts are capped at `5` total attempts.
- The webhook stores normalized fields only; never store the raw webhook, request signature, headers, API keys, or remote error bodies.
- The same Chatwoot `conversation_id` is processed serially; different conversations may run concurrently.
- Persist the prepared action before any Chatwoot customer-visible side effect.
- Keep existing local RAG, handoff, operations, document ingestion, and dashboard behavior working.
- Follow TDD for every behavior change and commit only files listed by the current task.

---

## File Map

**Create**

- `app/job_models.py`: immutable queue, prepared-action, and metric types.
- `app/job_store.py`: SQLite schema, enqueue, atomic claim, stage transitions, leases, heartbeat, cursor, dependency status, metrics, and failed-job replay.
- `app/inbound_queue.py`: one normalized idempotent enqueue entry point shared by webhook and reconciliation.
- `app/job_processor.py`: generation/delivery stage orchestration and error classification.
- `app/worker.py`: Worker supervisor, thread loops, heartbeat, reconciliation scheduling, and admin retry CLI.
- `app/chatwoot_reconciliation.py`: cursor-based recovery of missed Chatwoot incoming messages.
- `scripts/run-worker.ps1`: local Worker launcher using the existing `.env` rules.
- `scripts/retry-customer-service-job.ps1`: local-only failed-job replay wrapper.
- `tests/test_job_models.py`, `tests/test_job_store.py`, `tests/test_inbound_queue.py`, `tests/test_job_processor.py`, `tests/test_worker.py`, `tests/test_chatwoot_reconciliation.py`: focused unit tests.
- `tests/chatwoot-real-webhook-e2e.ps1`: real Chatwoot public-message delivery test.
- `tests/compose-worker-config-test.ps1`: rendered Compose assertion for the Worker command, ports, and shared volume.

**Modify**

- `app/settings.py`: add strict Worker settings.
- `app/chatwoot_webhook.py`: enqueue instead of executing `CustomerService.handle`.
- `app/main.py`: wire queue store, return 202/200, expose `/ready`.
- `app/customer_service.py`: split preparation from idempotent delivery while retaining the synchronous compatibility wrapper.
- `app/chatwoot_client.py`: support stable `source_id`, reply lookup, conversation listing, and message listing.
- `app/operations_api.py`, `app/static/operations.html`: expose queue, Worker, and dependency health.
- `compose.yaml`: run API and Worker from the same image and volume.
- `.env.example`: document Worker configuration without secrets.
- `README.md`: document API/Worker startup, recovery, and acceptance commands.
- Existing related tests under `tests/`: update fakes and queued response expectations.

---

### Task 1: Queue domain types and Worker settings

**Files:**
- Create: `app/job_models.py`
- Create: `tests/test_job_models.py`
- Modify: `app/settings.py`
- Modify: `tests/test_settings.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `JobStage`, `ResumeStage`, `Decision`, `PreparedAction`, `DurableJob`, `EnqueueResult`, `QueueMetrics`, and `WorkerSettings.from_env()`.
- Consumes: `Outcome` and `Reason` from `app.operations_models`.

- [ ] **Step 1: Write failing domain and settings tests**

Add `tests/test_job_models.py` with exact constructor and validation expectations:

```python
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
```

Extend `tests/test_settings.py` with a valid default and invalid-boundary test:

```python
from app.settings import WorkerSettings


class WorkerSettingsTests(unittest.TestCase):
    def test_defaults_match_single_server_design(self) -> None:
        settings = WorkerSettings.from_env({})

        self.assertEqual(settings.concurrency, 2)
        self.assertEqual(settings.lease_seconds, 300)
        self.assertEqual(settings.reconcile_interval_seconds, 60)
        self.assertEqual(settings.generation_max_attempts, 2)
        self.assertEqual(settings.delivery_max_attempts, 5)

    def test_rejects_out_of_range_values(self) -> None:
        invalid = {
            "CUSTOMER_SERVICE_WORKER_CONCURRENCY": "0",
            "CUSTOMER_SERVICE_JOB_LEASE_SECONDS": "30",
        }

        with self.assertRaisesRegex(ValueError, "Worker configuration is invalid"):
            WorkerSettings.from_env(invalid)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_job_models.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_settings.py' -v
```

Expected: imports fail because `app.job_models` and `WorkerSettings` do not exist.

- [ ] **Step 3: Implement immutable domain types and strict settings**

Create `app/job_models.py` with these exact public types and factory signatures:

```python
from dataclasses import dataclass
from typing import Literal

from app.operations_models import Outcome, Reason

JobStage = Literal[
    "queued",
    "generating",
    "ready_to_deliver",
    "delivering",
    "retry_wait",
    "completed",
    "failed",
]
ResumeStage = Literal["generating", "delivering"]
Decision = Literal["reply", "handoff", "ignore"]


@dataclass(frozen=True)
class PreparedAction:
    decision: Decision
    outcome: Outcome
    customer_text: str | None
    dify_conversation_id: str | None
    reason: Reason | None
    record_knowledge_gap: bool

    @classmethod
    def reply(cls, text: str, conversation_id: str) -> "PreparedAction":
        return cls("reply", "replied", text.strip(), conversation_id.strip(), None, False)

    @classmethod
    def handoff(cls, text: str, reason: Reason) -> "PreparedAction":
        return cls("handoff", "handed_off", text.strip(), None, reason, reason == "knowledge_gap")

    @classmethod
    def ignore_human_mode(cls) -> "PreparedAction":
        return cls("ignore", "ignored_human_mode", None, None, None, False)


@dataclass(frozen=True)
class EnqueueResult:
    status: Literal["queued", "duplicate"]
    job_id: int | None


@dataclass(frozen=True)
class DurableJob:
    id: int
    message_id: int
    account_id: int
    conversation_id: int
    sender_id: int
    content: str
    labels: tuple[str, ...]
    stage: JobStage
    resume_stage: ResumeStage | None
    action: PreparedAction | None
    outgoing_source_id: str
    generation_attempts: int
    delivery_attempts: int
    processing_duration_ms: int
    available_at: int
    locked_at: int | None
    locked_by: str | None
    last_error_code: str | None


@dataclass(frozen=True)
class QueueMetrics:
    queued: int
    processing: int
    retry_wait: int
    failed: int
    oldest_wait_seconds: int
    last_worker_heartbeat: int | None
```

Add `WorkerSettings` to `app/settings.py` with fields matching the environment names in `.env.example`. Parse integers, require concurrency `1..8`, lease `60..3600`, reconciliation interval `10..3600`, generation attempts `1..5`, and delivery attempts `1..10`. Raise only `ValueError("Worker configuration is invalid.")` for invalid input.

Add these exact defaults to `.env.example`:

```dotenv
CUSTOMER_SERVICE_WORKER_CONCURRENCY=2
CUSTOMER_SERVICE_JOB_LEASE_SECONDS=300
CUSTOMER_SERVICE_RECONCILE_INTERVAL_SECONDS=60
CUSTOMER_SERVICE_GENERATION_MAX_ATTEMPTS=2
CUSTOMER_SERVICE_DELIVERY_MAX_ATTEMPTS=5
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the three commands from Step 2.

Expected: all `PreparedActionTests`, existing settings tests, and new `WorkerSettingsTests` pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add app/job_models.py app/settings.py .env.example tests/test_job_models.py tests/test_settings.py
git commit -m "feat: define durable job model and worker settings"
```

---

### Task 2: SQLite durable job store

**Files:**
- Create: `app/job_store.py`
- Create: `tests/test_job_store.py`

**Interfaces:**
- Consumes: `IncomingMessage`, `PreparedAction`, `DurableJob`, `EnqueueResult`, `QueueMetrics`, `ResumeStage`.
- Produces: `SQLiteJobStore.enqueue()`, `claim_next()`, `save_prepared()`, `schedule_retry()`, `complete()`, `fail()`, `recover_expired_leases()`, `record_heartbeat()`, `metrics()`, reconciliation cursor methods, dependency status methods, and `retry_failed_message()`.

- [ ] **Step 1: Write failing enqueue, ordering, lease, and transition tests**

Create `tests/test_job_store.py` using `TemporaryDirectory` and `IncomingMessage`. Cover these exact public calls:

```python
first = store.enqueue(message(message_id=101, conversation_id=7), now=1_000)
duplicate = store.enqueue(message(message_id=101, conversation_id=7), now=1_001)
self.assertEqual((first.status, duplicate.status), ("queued", "duplicate"))

store.enqueue(message(message_id=102, conversation_id=7), now=1_002)
store.enqueue(message(message_id=103, conversation_id=8), now=1_003)
job_one = store.claim_next("worker-a", now=1_004, lease_seconds=300)
job_two = store.claim_next("worker-b", now=1_004, lease_seconds=300)
self.assertEqual(job_one.message_id, 101)
self.assertEqual(job_two.message_id, 103)

store.save_prepared(
    job_one.id,
    PreparedAction.reply("答复", "dify-101"),
    processing_duration_ms=120,
    now=1_005,
)
delivery = store.claim_next("worker-a", now=1_006, lease_seconds=300)
self.assertEqual(delivery.stage, "delivering")
store.complete(delivery.id, now=1_007)
next_same_conversation = store.claim_next("worker-a", now=1_008, lease_seconds=300)
self.assertEqual(next_same_conversation.message_id, 102)
```

Also test:

- an existing `processed_messages.status='completed'` row returns `duplicate` without creating a job;
- a generating lease is not reclaimed before 300 seconds;
- `recover_expired_leases(now=1_400, lease_seconds=300)` moves expired work to `retry_wait` with `resume_stage='generating'`;
- `failed` blocks a later job in the same conversation;
- `retry_failed_message(message_id, now)` restores the failed job to its saved resume stage;
- heartbeat and metrics return stable integer values;
- first reconciliation cursor initialization performs no historical scan marker;
- dependency status stores only `ok`, `error_code`, and timestamps.

- [ ] **Step 2: Run store tests and verify RED**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_job_store.py' -v
```

Expected: import fails because `SQLiteJobStore` does not exist.

- [ ] **Step 3: Implement schema and enqueue transaction**

Create `app/job_store.py`. Use `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, a five-second SQLite timeout, and `BEGIN IMMEDIATE` for claim/replay transactions. The schema must include these checks and unique keys:

```sql
CREATE TABLE IF NOT EXISTS inbound_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE,
    account_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '[]',
    stage TEXT NOT NULL CHECK(stage IN (
        'queued','generating','ready_to_deliver','delivering',
        'retry_wait','completed','failed'
    )),
    resume_stage TEXT CHECK(resume_stage IN ('generating','delivering')),
    decision TEXT CHECK(decision IN ('reply','handoff','ignore')),
    outcome TEXT,
    reply_text TEXT,
    dify_conversation_id TEXT,
    reason TEXT,
    record_knowledge_gap INTEGER NOT NULL DEFAULT 0 CHECK(record_knowledge_gap IN (0,1)),
    outgoing_source_id TEXT NOT NULL,
    generation_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    processing_duration_ms INTEGER NOT NULL DEFAULT 0,
    available_at INTEGER NOT NULL,
    locked_at INTEGER,
    locked_by TEXT,
    last_error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_jobs_ready
    ON inbound_jobs(stage, available_at, id);
CREATE INDEX IF NOT EXISTS idx_inbound_jobs_conversation
    ON inbound_jobs(conversation_id, id, stage);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('processing', 'completed')),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    heartbeat_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_state (
    name TEXT PRIMARY KEY,
    cursor_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dependency_status (
    name TEXT PRIMARY KEY CHECK(name IN ('dify','chatwoot')),
    ok INTEGER NOT NULL CHECK(ok IN (0,1)),
    error_code TEXT,
    checked_at INTEGER NOT NULL
);
```

`enqueue()` must first check `processed_messages` for a completed legacy record, then run `INSERT OR IGNORE`. Set `outgoing_source_id` to `f"ai-cs:{message.message_id}:reply"` and serialize only `message.labels` as JSON.

Use these exact transition signatures: `enqueue(message: IncomingMessage, *, now: int | None = None) -> EnqueueResult`, `claim_next(worker_id: str, *, now: int, lease_seconds: int) -> DurableJob | None`, `save_prepared(job_id: int, action: PreparedAction, *, processing_duration_ms: int, now: int) -> None`, `schedule_retry(job_id: int, *, resume_stage: ResumeStage, error_code: str, available_at: int, now: int) -> None`, `complete(job_id: int, *, now: int) -> None`, and `fail(job_id: int, *, resume_stage: ResumeStage, error_code: str, now: int) -> None`.

- [ ] **Step 4: Implement atomic claim and stage transitions**

Use this eligibility rule inside the same `BEGIN IMMEDIATE` transaction that updates the selected row:

```sql
SELECT j.id
FROM inbound_jobs AS j
WHERE j.stage IN ('queued', 'ready_to_deliver', 'retry_wait')
  AND j.available_at <= ?
  AND NOT EXISTS (
      SELECT 1
      FROM inbound_jobs AS earlier
      WHERE earlier.conversation_id = j.conversation_id
        AND earlier.id < j.id
        AND earlier.stage NOT IN ('completed')
  )
ORDER BY j.id
LIMIT 1
```

Map `queued` to `generating`, `ready_to_deliver` to `delivering`, and `retry_wait` to its non-null `resume_stage`. Increment the matching attempt counter when claiming. `save_prepared()` must atomically write every `PreparedAction` field, clear the lease, and set `stage='ready_to_deliver'`. `complete()` must set the job to `completed`, clear the lease, and upsert `processed_messages` to `completed` in the same transaction.

`schedule_retry()` must require a stable snake-case error code matching `^[a-z][a-z0-9_]{0,63}$`, write `stage='retry_wait'`, set `resume_stage`, and clear the lease. `fail()` must preserve the resume stage and set `stage='failed'`.

- [ ] **Step 5: Implement recovery, cursor, heartbeat, dependency, and metric methods**

Implement these exact public signatures: `recover_expired_leases(*, now: int, lease_seconds: int) -> int`, `record_heartbeat(worker_id: str, *, now: int) -> None`, `metrics(*, now: int) -> QueueMetrics`, `get_reconciliation_cursor() -> int | None`, `initialize_reconciliation_cursor(*, now: int) -> bool`, `advance_reconciliation_cursor(*, expected: int, new_value: int) -> bool`, `record_dependency_status(name: Literal["dify", "chatwoot"], *, ok: bool, error_code: str | None, checked_at: int) -> None`, `dependency_statuses() -> dict[str, dict[str, object]]`, and `retry_failed_message(message_id: int, *, now: int) -> bool`.

Every method must use a concrete SQL transaction. `metrics()` counts `generating` and `delivering` as processing and computes `oldest_wait_seconds=max(0, now-min(created_at))` across `queued` and `retry_wait` jobs.

- [ ] **Step 6: Run focused and existing store tests**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_job_store.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_conversation_store.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_operations_store.py' -v
```

Expected: all tests pass and no existing SQLite table is dropped.

- [ ] **Step 7: Commit Task 2**

```powershell
git add app/job_store.py tests/test_job_store.py
git commit -m "feat: add SQLite durable customer message queue"
```

---

### Task 3: Fast enqueue-only webhook

**Files:**
- Create: `app/inbound_queue.py`
- Create: `tests/test_inbound_queue.py`
- Modify: `app/chatwoot_webhook.py`
- Modify: `app/main.py`
- Modify: `tests/test_chatwoot_webhook.py`
- Modify: `tests/test_operations_api.py`

**Interfaces:**
- Consumes: `SQLiteJobStore.enqueue(IncomingMessage) -> EnqueueResult`.
- Produces: `InboundQueue.enqueue(message)`, `WebhookProcessingResult.status` values `queued`, `duplicate`, and `ignored`.

- [ ] **Step 1: Write failing queue and webhook latency tests**

Create `tests/test_inbound_queue.py` with a fake store and best-effort duplicate metric recorder. Update `tests/test_chatwoot_webhook.py` so a valid new webhook expects `queued`, a second call expects `duplicate`, and the handler protocol exposes `enqueue()` instead of `handle()`.

Add a FastAPI test that patches `app.main.get_inbound_queue` with a fake whose `enqueue` returns immediately while a fake Dify object raises if touched:

```python
started = time.perf_counter()
response = client.post(
    "/v1/chatwoot/webhook",
    content=body,
    headers=signed_headers(body),
)
elapsed = time.perf_counter() - started

self.assertEqual(response.status_code, 202)
self.assertEqual(response.json(), {"status": "queued"})
self.assertLess(elapsed, 1.0)
self.assertEqual(fake_queue.messages, [expected_message])
```

- [ ] **Step 2: Run webhook tests and verify RED**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_inbound_queue.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_chatwoot_webhook.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_operations_api.py' -v
```

Expected: tests fail because the current route invokes the synchronous customer service.

- [ ] **Step 3: Implement the shared inbound queue service**

Create `InboundQueue` with this public surface:

```python
class InboundQueue:
    def __init__(self, store: SQLiteJobStore, operations: OperationsRecorder) -> None:
        self._store = store
        self._operations = operations

    def enqueue(self, message: IncomingMessage) -> EnqueueResult:
        result = self._store.enqueue(message)
        if result.status == "duplicate":
            try:
                self._operations.record_event(
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    outcome="duplicate",
                    reason=None,
                    duration_ms=0,
                )
            except Exception:
                logging.getLogger(__name__).warning(
                    "Duplicate metric recording failed.", exc_info=True
                )
        return result
```

Both webhook and reconciler must call this class; neither may call `SQLiteJobStore.enqueue()` directly.

- [ ] **Step 4: Change webhook processing and FastAPI response codes**

Change `process_chatwoot_webhook()` to accept an `IncomingMessageEnqueuer` protocol and return the `EnqueueResult.status`. In `app/main.py`, cache `SQLiteJobStore` and `InboundQueue` by database path, and use FastAPI `Response` to set status 202 only for `queued`; keep 200 for `duplicate` and `ignored`.

Do not construct `CustomerService`, `DifyChatClient`, or `ChatwootClient` in the webhook route. Preserve 401, 413, 422, and 503 responses, and map `sqlite3.Error` to the existing generic 503 message without user content.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the three commands from Step 2.

Expected: all pass; latency test remains below one second.

- [ ] **Step 6: Commit Task 3**

```powershell
git add app/inbound_queue.py app/chatwoot_webhook.py app/main.py tests/test_inbound_queue.py tests/test_chatwoot_webhook.py tests/test_operations_api.py
git commit -m "feat: enqueue Chatwoot webhooks before processing"
```

---

### Task 4: Prepared actions and idempotent Chatwoot delivery

**Files:**
- Modify: `app/customer_service.py`
- Modify: `app/chatwoot_client.py`
- Modify: `tests/test_customer_service.py`
- Modify: `tests/test_chatwoot_client.py`
- Modify: `tests/test_operations_integration.py`

**Interfaces:**
- Consumes: `PreparedAction` and current handoff/Dify/conversation abstractions.
- Produces: `CustomerService.prepare()`, `dify_error_action()`, `deliver()`, `record_result()`, `ChatwootClient.find_outgoing_message_by_source_id()`, and optional `source_id` on send.

- [ ] **Step 1: Write failing preparation and idempotent-delivery tests**

Add tests proving:

```python
action = service.prepare(incoming_message("现货多久发货？"))
self.assertEqual(action, PreparedAction.reply("48 小时内发货。", "dify-7"))
self.assertEqual(chatwoot.sent_messages, [])

chatwoot.existing_source_ids["ai-cs:101:reply"] = 9001
service.deliver(message, action, source_id="ai-cs:101:reply")
self.assertEqual(chatwoot.sent_messages, [])
self.assertEqual(store.get_conversation(message.conversation_id).mode, "ai")

handoff = PreparedAction.handoff("正在转接人工。", "explicit_human_request")
chatwoot.raise_on_label = True
with self.assertRaises(ChatwootAPIError):
    service.deliver(message, handoff, source_id="ai-cs:102:reply")
self.assertIsNone(store.get_conversation(message.conversation_id))
```

Extend `tests/test_chatwoot_client.py` to assert `source_id` is included in the POST JSON and `find_outgoing_message_by_source_id()` returns the matching public outgoing message ID while ignoring private, incoming, and mismatched messages.

Update the Chatwoot fake in `tests/test_operations_integration.py` so `send_outgoing_message()` accepts keyword-only `source_id`, records it, and `find_outgoing_message_by_source_id()` returns `None`. Do this in the failing-test step so the integration test remains runnable while the production protocol changes.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_customer_service.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_chatwoot_client.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_operations_integration.py' -v
```

Expected: methods and `source_id` parameter are missing.

- [ ] **Step 3: Add Chatwoot idempotent message primitives**

Change the public signatures to `send_outgoing_message(account_id: int, conversation_id: int, content: str, *, source_id: str | None = None) -> int` and `find_outgoing_message_by_source_id(account_id: int, conversation_id: int, source_id: str) -> int | None`.

Keep the existing validation and add these exact rules: include `source_id` only when non-empty; GET `/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages`; inspect `payload`; accept only `message_type` equal to integer `1` or string `outgoing`, `private` false, exact `source_id`, and a positive integer `id`.

- [ ] **Step 4: Refactor customer service into prepare and deliver phases**

Implement these exact behaviors:

```python
def prepare(self, message: IncomingMessage) -> PreparedAction:
    state = self._store.get_conversation(message.conversation_id)
    if state is not None and state.mode == "human":
        return PreparedAction.ignore_human_mode()
    rule_reason = handoff_reason(message.content)
    if rule_reason is not None:
        return PreparedAction.handoff(self._DEFAULT_HANDOFF_TEXT, cast(Reason, rule_reason))
    response = self._dify.send_message(
        query=message.content,
        user=f"chatwoot:{message.sender_id}",
        conversation_id=(state.dify_conversation_id if state else None),
    )
    parsed = parse_dify_answer(response.answer)
    if parsed.needs_human:
        reason: Reason = (
            "knowledge_gap"
            if response.answer.strip().upper().startswith("[HANDOFF]")
            else "dify_error"
        )
        return PreparedAction.handoff(parsed.customer_text, reason)
    return PreparedAction.reply(parsed.customer_text, response.conversation_id)
```

`deliver()` must return without side effects for `ignore`; for reply/handoff, look up the stable source ID before sending. For handoff, require label success before saving local human mode. For reply, save Dify conversation state only after the customer-visible message exists.

Retain `handle()` as a compatibility wrapper that uses `prepare()`, converts `DifyChatAPIError` to `dify_error_action()`, calls `deliver()`, completes/releases the legacy message claim, and records the existing operation event. Remove the swallowed `ChatwootAPIError` around label updates.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the two commands from Step 2.

Expected: all existing synchronous behavior remains compatible; new preparation has no Chatwoot side effect; delivery is source-ID idempotent.

- [ ] **Step 6: Commit Task 4**

```powershell
git add app/customer_service.py app/chatwoot_client.py tests/test_customer_service.py tests/test_chatwoot_client.py tests/test_operations_integration.py
git commit -m "feat: persist actions before idempotent Chatwoot delivery"
```

---

### Task 5: Job stage processor and retry policy

**Files:**
- Create: `app/job_processor.py`
- Create: `tests/test_job_processor.py`
- Modify: `app/job_store.py`
- Modify: `app/customer_service.py`
- Modify: `tests/test_customer_service.py`

**Interfaces:**
- Consumes: claimed `DurableJob`, `CustomerService`, `SQLiteJobStore`, `WorkerSettings`.
- Produces: `JobProcessor.process(job)` and stable `retry_delay_seconds(attempt)`.

- [ ] **Step 1: Write failing staged-processing tests**

Create tests with fake service/store for these cases:

```python
processor.process(generating_job(attempts=1))
self.assertEqual(store.saved_actions[0].decision, "reply")
self.assertEqual(service.delivery_calls, [])

service.prepare_error = DifyChatAPIError(None, "timeout")
processor.process(generating_job(attempts=1))
self.assertEqual(store.retries[0].resume_stage, "generating")
self.assertEqual(store.retries[0].error_code, "dify_timeout")

processor.process(generating_job(attempts=2))
self.assertEqual(store.saved_actions[-1].reason, "dify_error")

processor.process(delivering_job(attempts=1, action=prepared_reply()))
self.assertEqual(service.prepare_calls, [])
self.assertEqual(store.completed_job_ids, [55])
```

Also test Chatwoot 401 becomes `failed`, Chatwoot 503 schedules delivery retry, label failure maps to `label_update_failed`, and delays are exactly `[5, 30, 120, 600]` for attempts 1 through 4.

- [ ] **Step 2: Run processor tests and verify RED**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_job_processor.py' -v
```

Expected: `app.job_processor` does not exist.

- [ ] **Step 3: Implement generation processing**

Create `JobProcessor`. For `job.stage == "generating"`, measure processing time with an injected monotonic clock. Call `CustomerService.prepare()` once. On success, call `store.save_prepared()` and record Dify dependency success. Map errors without persisting remote text:

```python
def _dify_error_code(error: DifyChatAPIError) -> str:
    if error.status_code == 429:
        return "dify_rate_limited"
    if error.status_code in {401, 403}:
        return "dify_auth_error"
    if error.status_code is not None and error.status_code >= 500:
        return "dify_server_error"
    return "dify_timeout" if error.status_code is None else "dify_request_error"
```

For 401/403 or when `generation_attempts >= generation_max_attempts`, persist `CustomerService.dify_error_action()` instead of retrying. Otherwise schedule a generating retry.

- [ ] **Step 4: Implement delivery processing**

For `job.stage == "delivering"`, require `job.action` and call `CustomerService.deliver()` with `job.outgoing_source_id`. After delivery, call `record_result()` and `store.complete()`. Record Chatwoot dependency success.

`CustomerService.record_result(message, action, duration_ms)` records the final `action.outcome` and `action.reason` through the existing best-effort operations recorder. When `action.record_knowledge_gap` is true, it also calls the existing privacy-redacting knowledge-gap recorder exactly once per delivery attempt; summary queries continue selecting the last final event for a message.

Map `ChatwootAPIError` 401/403 to `store.fail(job.id, resume_stage="delivering", error_code="chatwoot_auth_error", now=clock())`. Retry other Chatwoot errors until `delivery_attempts >= delivery_max_attempts`, then fail. Classify a label error as `label_update_failed` by raising a dedicated `HandoffLabelError` from `CustomerService.deliver()` while retaining the original exception as its cause.

- [ ] **Step 5: Run processor and customer-service tests**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_job_processor.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_customer_service.py' -v
```

Expected: all pass; delivering tests assert no second Dify call.

- [ ] **Step 6: Commit Task 5**

```powershell
git add app/job_processor.py app/job_store.py app/customer_service.py tests/test_job_processor.py tests/test_customer_service.py
git commit -m "feat: process durable jobs in recoverable stages"
```

---

### Task 6: Worker supervisor, heartbeats, and failed-job replay

**Files:**
- Create: `app/worker.py`
- Create: `tests/test_worker.py`
- Create: `scripts/run-worker.ps1`
- Create: `scripts/retry-customer-service-job.ps1`

**Interfaces:**
- Consumes: `SQLiteJobStore.claim_next()`, `JobProcessor.process()`, `WorkerSettings`.
- Produces: `WorkerSupervisor.run(stop_event)`, `python -m app.worker`, and `--retry-message-id` admin mode.

- [ ] **Step 1: Write failing Worker loop tests**

Use fake store/processor and a real `threading.Event` to prove:

- exactly two thread loops are created for default settings;
- two different conversations can be passed to the processor before either is released;
- heartbeat is recorded by the supervisor independently of task duration;
- expired leases are recovered before claiming new work;
- no-job polling waits through an injected `wait(seconds)` callback instead of busy spinning;
- `retry_failed_job(store, message_id, now)` returns false for unknown/non-failed messages and true for a failed job.

- [ ] **Step 2: Run Worker tests and verify RED**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_worker.py' -v
```

Expected: Worker module is missing.

- [ ] **Step 3: Implement Worker supervisor and composition root**

Create one thread loop per concurrency slot. Each loop uses a stable worker ID formatted as `<hostname>:<pid>:<slot>`, then repeats:

```python
while not stop_event.is_set():
    job = store.claim_next(worker_id, now=clock(), lease_seconds=settings.lease_seconds)
    if job is None:
        stop_event.wait(0.25)
        continue
    processor.process(job)
```

The supervisor main loop calls `recover_expired_leases()` and writes a supervisor heartbeat every 15 seconds. Trap `SIGINT` and `SIGTERM`, set the stop event, stop claiming new jobs, and wait up to the lease duration for active threads.

Build production dependencies from `Settings.from_env()`, `WorkerSettings.from_env()`, `SQLiteJobStore`, `SQLiteConversationStore`, `DifyChatClient`, `ChatwootClient`, `OperationsStore`, `CustomerService`, and `JobProcessor`. Do not import `app.main` from the Worker.

- [ ] **Step 4: Implement local launch and replay scripts**

`scripts/run-worker.ps1` must use the same `.env` parsing loop as `scripts/run-docling.ps1`, run `& $pythonExecutable -m app.worker`, and never print environment values.

`scripts/retry-customer-service-job.ps1` accepts a mandatory positive `MessageId`, loads only `CUSTOMER_SERVICE_DB_PATH`, and runs:

```python
from app.job_store import SQLiteJobStore

store = SQLiteJobStore(sys.argv[1])
if not store.retry_failed_message(int(sys.argv[2]), now=int(time.time())):
    raise SystemExit("The failed customer-service job was not found.")
```

On success print only `PASS: Failed customer-service job was queued for retry.`

- [ ] **Step 5: Run Worker tests and a configuration-safe script smoke test**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_worker.py' -v
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/retry-customer-service-job.ps1 -MessageId 999999
```

Expected: unit tests pass; replay command exits non-zero with the stable not-found message and prints no configuration value.

- [ ] **Step 6: Commit Task 6**

```powershell
git add app/worker.py scripts/run-worker.ps1 scripts/retry-customer-service-job.ps1 tests/test_worker.py
git commit -m "feat: run durable customer-service worker"
```

---

### Task 7: Chatwoot message reconciliation

**Files:**
- Create: `app/chatwoot_reconciliation.py`
- Create: `tests/test_chatwoot_reconciliation.py`
- Modify: `app/chatwoot_client.py`
- Modify: `app/worker.py`
- Modify: `tests/test_chatwoot_client.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Consumes: `InboundQueue.enqueue()`, reconciliation cursor methods, Chatwoot account API.
- Produces: `ChatwootClient.list_conversations_page()`, `list_conversation_messages()`, and `ChatwootReconciler.run_once(now)`.

- [ ] **Step 1: Write failing Chatwoot list and reconciler tests**

Test `list_conversations_page(account_id, page)` against `{"data":{"payload":[{"id":7,"labels":[],"last_activity_at":1000}]}}` and `list_conversation_messages()` against `{"payload":[{"id":501,"message_type":0,"private":false,"content_type":"text","content":"退款多久到帐？","created_at":1001,"sender":{"id":9}}]}`. Reject malformed shapes with `ChatwootAPIError`.

Create reconciler tests proving:

```python
self.assertEqual(reconciler.run_once(now=1_000), 0)
self.assertEqual(store.get_reconciliation_cursor(), 1_000)
self.assertEqual(chatwoot.list_calls, [])

store_with_cursor = FakeJobStore(cursor=1_000)
reconciler_with_cursor = ChatwootReconciler(
    account_id=2,
    store=store_with_cursor,
    queue=queue,
    chatwoot=chatwoot,
)
count = reconciler_with_cursor.run_once(now=1_060)
self.assertEqual(count, 1)
self.assertEqual(queue.messages[0].message_id, 501)
self.assertEqual(store_with_cursor.get_reconciliation_cursor(), 1_060)
```

Also test a five-minute overlap, incoming/public/text filtering, exact account validation, duplicate queue results, pagination stop when `last_activity_at` is older than cutoff, and cursor preservation when any Chatwoot request fails.

- [ ] **Step 2: Run reconciliation tests and verify RED**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_chatwoot_reconciliation.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_chatwoot_client.py' -v
```

Expected: list and reconciler methods are missing.

- [ ] **Step 3: Add paginated Chatwoot read methods**

Add immutable `ChatwootConversation` with `id`, `labels`, and `last_activity_at`. `list_conversations_page()` calls:

```text
GET /api/v1/accounts/{account_id}/conversations?status=all&sort_by=last_activity_at_desc&page={page}
```

`list_conversation_messages()` calls the existing conversation message endpoint and returns only dictionary payload items. Reuse `_request_json`; never log response payloads.

- [ ] **Step 4: Implement cursor-based reconciliation**

Use a fixed overlap of 300 seconds. On missing cursor, initialize it to `now` and return without listing Chatwoot. Otherwise list newest conversations until a page is empty or every conversation on the page is older than `cursor-300`. For qualifying messages, build `IncomingMessage` only when:

- `message_type` is integer `0` or string `incoming`;
- `private` is false;
- `content_type` is absent or `text`;
- IDs are positive integers and content is non-empty;
- message `created_at >= cursor-300`.

Call `InboundQueue.enqueue()` for each candidate. Advance the cursor from the exact previous value to `now` only after all pages succeed.

- [ ] **Step 5: Schedule reconciliation from the Worker supervisor**

Inject `ChatwootReconciler` into `WorkerSupervisor`. Run it once at startup and then every `reconcile_interval_seconds`. Reconciliation errors record Chatwoot dependency failure and log a stable message without payloads; they do not stop job threads or advance the cursor.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the commands from Step 2 plus:

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_worker.py' -v
```

Expected: all list, cursor, overlap, and scheduler tests pass.

- [ ] **Step 7: Commit Task 7**

```powershell
git add app/chatwoot_reconciliation.py app/chatwoot_client.py app/worker.py tests/test_chatwoot_reconciliation.py tests/test_chatwoot_client.py tests/test_worker.py
git commit -m "feat: reconcile missed Chatwoot messages"
```

---

### Task 8: Readiness and queue operations visibility

**Files:**
- Modify: `app/main.py`
- Modify: `app/job_store.py`
- Modify: `app/operations_api.py`
- Modify: `app/static/operations.html`
- Modify: `tests/test_operations_api.py`
- Modify: `tests/test_operations_dashboard.py`
- Modify: `tests/operations-dashboard-browser.py`

**Interfaces:**
- Consumes: `SQLiteJobStore.metrics()`, heartbeat, and dependency status.
- Produces: `GET /ready` and authenticated `GET /v1/operations/queue`.

- [ ] **Step 1: Write failing readiness and operations API tests**

Add tests for:

```python
ready = client.get("/ready")
self.assertEqual(ready.status_code, 200)
self.assertEqual(ready.json()["status"], "ready")

queue = client.get("/v1/operations/queue", headers=operations_headers())
self.assertEqual(queue.status_code, 200)
self.assertEqual(queue.json()["queue"]["failed"], 1)
self.assertIn("dify", queue.json()["dependencies"])
self.assertIn("chatwoot", queue.json()["dependencies"])
```

Patch the clock so a missing heartbeat and a heartbeat older than 45 seconds both make `/ready` return 503 with `{"status":"not_ready"}`. Preserve 401 for a missing operations key.

Add dashboard markup assertions for `data-queue-health`, `data-worker-heartbeat`, and `data-failed-jobs`.

- [ ] **Step 2: Run API/dashboard tests and verify RED**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_operations_api.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_operations_dashboard.py' -v
```

Expected: endpoints and dashboard elements do not exist.

- [ ] **Step 3: Implement readiness and protected queue status**

Add `SQLiteJobStore.is_writable()` that begins an immediate transaction, updates a dedicated `queue_meta` row to its existing value, and rolls back. `/ready` returns 200 only when settings load, the database is writable, and the newest Worker heartbeat is at most 45 seconds old. It does not call Dify or Chatwoot synchronously.

Add authenticated `/v1/operations/queue` returning:

```json
{
  "queue": {
    "queued": 0,
    "processing": 0,
    "retry_wait": 0,
    "failed": 0,
    "oldest_wait_seconds": 0,
    "last_worker_heartbeat": 0
  },
  "dependencies": {
    "dify": {"ok": true, "error_code": null, "checked_at": 0},
    "chatwoot": {"ok": true, "error_code": null, "checked_at": 0}
  }
}
```

Use actual integer/null values from the store; do not expose job contents, conversation IDs, Worker IDs, or exception messages.

- [ ] **Step 4: Add compact queue status to the operations dashboard**

Load `/v1/operations/queue` with the existing operations key. Show queue depth, oldest wait, failed count, Worker heartbeat freshness, and Dify/Chatwoot status. Keep the current dashboard layout responsive at 360 px; use text status and existing colors rather than introducing a chart library.

- [ ] **Step 5: Run unit and browser tests**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_operations_api.py' -v
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_operations_dashboard.py' -v
python ./tests/operations-dashboard-browser.py
```

Expected: unit tests pass; browser output remains `PASS: Operations dashboard desktop, mobile, auth, filters, and CSV are verified.` after extending it to queue health.

- [ ] **Step 6: Commit Task 8**

```powershell
git add app/main.py app/job_store.py app/operations_api.py app/static/operations.html tests/test_operations_api.py tests/test_operations_dashboard.py tests/operations-dashboard-browser.py
git commit -m "feat: expose queue readiness and operations health"
```

---

### Task 9: Runtime wiring, real Chatwoot E2E, and full regression

**Files:**
- Modify: `compose.yaml`
- Modify: `README.md`
- Modify: `tests/customer-service-e2e.ps1`
- Create: `tests/chatwoot-real-webhook-e2e.ps1`
- Create: `tests/compose-worker-config-test.ps1`

**Interfaces:**
- Consumes: API command, `python -m app.worker`, Chatwoot Website Token public API, and current acceptance scripts.
- Produces: repeatable local/Docker startup and one true Chatwoot-to-Worker end-to-end test.

- [ ] **Step 1: Add a failing Compose/config assertion**

Create `tests/compose-worker-config-test.ps1`. Render `docker compose config --format json`, parse it with `ConvertFrom-Json`, and assert:

```powershell
if ($config.services.worker.command -join ' ' -notmatch 'python -m app.worker') {
    throw 'Worker service command is missing.'
}
if ($config.services.worker.ports) {
    throw 'Worker service must not publish ports.'
}
if (-not $config.services.worker.volumes) {
    throw 'Worker service must share the durable SQLite volume.'
}
```

- [ ] **Step 2: Update Compose to run API and Worker**

Extract shared image/build/environment/volume configuration into a YAML anchor. Keep the existing `docling` API service name and localhost port. Add `worker` with:

```yaml
worker:
  <<: *customer-service-base
  command: ["python", "-m", "app.worker"]
  healthcheck:
    disable: true
```

Do not publish Worker ports. Both services mount `./.runtime/customer-service:/app/.runtime/customer-service`.

- [ ] **Step 3: Update the direct service E2E for queued semantics**

Change `Send-SignedWebhook` assertions from immediate `replied`/`handed_off` to `queued` or `duplicate`. Poll Chatwoot and operations until the final behavior appears. Fix knowledge-gap lookup to use `last_conversation_id` rather than a GUID substring so privacy redaction cannot make the test random.

Run it three times:

```powershell
1..3 | ForEach-Object {
    powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/customer-service-e2e.ps1
}
```

Expected: all three runs print the customer-service PASS line.

- [ ] **Step 4: Add a true Chatwoot webhook E2E**

Create `tests/chatwoot-real-webhook-e2e.ps1`. Read `CHATWOOT_WEBSITE_TOKEN` without printing it. Use the Chatwoot public API in this order:

```text
POST /public/api/v1/inboxes/{website_token}/contacts
POST /public/api/v1/inboxes/{website_token}/contacts/{source_id}/conversations
POST /public/api/v1/inboxes/{website_token}/contacts/{source_id}/conversations/{display_id}/messages
```

Create a unique contact, send `退款一般多久到帐？`, and poll the authenticated account conversation messages until exactly one public outgoing answer contains both the platform processing time and bank/payment arrival time. Do not call `/v1/chatwoot/webhook` from this test. Read `CHATWOOT_ACCOUNT_ID` and `CHATWOOT_API_TOKEN` only for authenticated polling and cleanup. Delete the test conversation and contact through the account API in `finally`, and null all tokens and headers.

- [ ] **Step 5: Update README startup and recovery instructions**

Document two required local terminals:

```powershell
./scripts/run-docling.ps1
./scripts/run-worker.ps1
```

Document Docker startup:

```powershell
docker compose up -d --build docling worker
docker compose ps
```

Explain queued webhook semantics, `/ready`, the protected queue panel, failed-job replay, and that API without Worker can accept messages only until operators notice backlog. Replace the claim “已经跑通真实业务链路” with “已通过本地真实组件链路与自动化验收，尚待真实商家灰度验证”.

- [ ] **Step 6: Run complete automated verification**

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_*.py'
./.venv/Scripts/python.exe -m compileall -q app tests
./.venv/Scripts/python.exe -m pip check
powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/configure-dify-env-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/dify-chatflow-dsl-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/configure-chatwoot-env-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/configure-operations-env-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/compose-worker-config-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/rag-evaluation.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/customer-service-e2e.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./tests/chatwoot-real-webhook-e2e.ps1
python ./tests/operations-dashboard-browser.py
docker compose config --quiet
```

Expected: every command exits zero; RAG remains 8/8; both E2E scripts and browser test print PASS; Compose validates.

- [ ] **Step 7: Verify runtime status and repository hygiene**

```powershell
Invoke-WebRequest http://127.0.0.1:5001/health -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:5001/ready -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:3000/ -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8081/ -UseBasicParsing
git status --short
git ls-files .env .runtime artifacts
```

Expected: service requests return 200; `git status --short` lists only intended Task 9 files before commit; the final `git ls-files` command prints nothing.

- [ ] **Step 8: Commit Task 9**

```powershell
git add compose.yaml README.md tests/customer-service-e2e.ps1 tests/chatwoot-real-webhook-e2e.ps1 tests/compose-worker-config-test.ps1
git commit -m "test: verify durable Chatwoot message processing"
```

---

## Final Review Gate

- [ ] Compare every implementation commit with `docs/superpowers/specs/2026-07-15-durable-webhook-processing-design.md`.
- [ ] Confirm no task added Redis, PostgreSQL, Celery, RQ, a new production package, or multi-server behavior.
- [ ] Confirm Chatwoot webhook code has no Dify or outbound Chatwoot call path.
- [ ] Confirm all remote errors are stored as stable codes only.
- [ ] Confirm a failed job blocks later jobs only in the same conversation.
- [ ] Confirm no raw webhook, signature, token, API key, or remote response body is stored or logged.
- [ ] Confirm all nine task commits contain only their listed files.
- [ ] Run the complete verification block once more from a clean worktree.
