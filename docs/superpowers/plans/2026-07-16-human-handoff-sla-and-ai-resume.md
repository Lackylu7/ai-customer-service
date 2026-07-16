# Human Handoff SLA and AI Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为人工接管会话增加持久化 5 分钟 SLA、安全 AI 临时托底、人工回复取消机制，以及运维看板中的“恢复 AI”操作。

**Architecture:** Chatwoot Webhook 同时接收入站客户消息和人工公开回复，二者先经过签名、账户与事件类型校验，再原子写入共享 SQLite。现有耐久任务队列新增 `human_wait` 和托底任务类型；Worker 到期后先重新检查人工活动、冷却期与会话模式，再选择知识库临时回答或固定等待提示。恢复 AI 由独立控制服务编排 Chatwoot 标签和 SQLite 事务，运维 API 与看板只暴露脱敏状态。

**Tech Stack:** Python 3.11、FastAPI、SQLite WAL、标准库 `urllib` Chatwoot 客户端、Dify Chat API、原生 HTML/CSS/JavaScript、`unittest`、FastAPI TestClient、Playwright、PowerShell E2E。

## Global Constraints

- 默认 SLA：`CUSTOMER_SERVICE_HUMAN_SLA_SECONDS=300`。
- 默认托底冷却：`CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS=300`。
- 会话所有权仍只有 `ai` 和 `human`；AI 托底不得自动结束人工接管。
- 只有持有 `OPERATIONS_API_KEY` 的人员可以查询人工会话和恢复 AI，恢复前必须二次确认。
- 普通知识库问题可临时托底；投诉、法律、支付、账户安全、主动要求人工、知识缺口和 Dify 故障只能发送等待提示。
- 人工回复必须取消尚未投递的托底；恢复 AI 后不得补答恢复前的迟到 Webhook 或对账消息。
- 所有 AI 出站消息使用 `ai-cs:` 稳定 `source_id`；托底使用 `ai-cs:<incoming_message_id>:human-fallback`。
- 列表、审计和日志不得包含客户正文、联系人信息、密钥、Webhook 签名、请求头或远程响应全文。
- 不新增生产依赖，不引入 Redis、Celery、RQ、PostgreSQL 或多服务器队列行为。
- 沿用现有 SQLite WAL、按会话严格顺序、租约恢复和 Chatwoot 幂等查询机制。

## File Structure

- Create: `app/handoff_schema.py` — 共享会话活动字段和控制审计表的幂等迁移。
- Create: `app/handoff_control.py` — 人工会话列表模型、恢复状态与 Chatwoot/SQLite 编排。
- Modify: `app/chatwoot_events.py` — 客户消息与人工回复的规范化事件。
- Modify: `app/chatwoot_webhook.py` — 单一签名入口分发客户消息和人工活动。
- Modify: `app/chatwoot_reconciliation.py` — 对账消息保留 Chatwoot 事件时间。
- Modify: `app/inbound_queue.py` — 同一事件接收器写入队列或记录人工回复。
- Modify: `app/settings.py` — SLA、冷却时间和运维恢复所需 Chatwoot 配置。
- Modify: `app/conversation_store.py` — 扩展会话状态并记录转人工上下文。
- Modify: `app/job_models.py` — `human_wait`、任务来源和托底任务类型。
- Modify: `app/job_store.py` — 队列迁移、等待/取消/托底完成、人工列表和恢复事务。
- Modify: `app/customer_service.py` — 安全托底准备与不改变会话模式的投递。
- Modify: `app/job_processor.py` — 到期托底分支、依赖状态、取消和重试。
- Modify: `app/chatwoot_client.py` — 移除单个标签和读取恢复截止消息 ID。
- Modify: `app/operations_api.py` — 人工会话查询与恢复 API。
- Modify: `app/static/operations.html` — 人工会话区域、状态、确认弹窗和恢复按钮。
- Modify: `app/main.py`, `app/worker.py` — 注入扩展配置与事件接收器。
- Modify: `.env.example`, `README.md` — 配置、使用和故障恢复说明。
- Create: `tests/test_handoff_control.py` — 恢复编排单元测试。
- Create: `tests/human-handoff-e2e.ps1` — 真实 Chatwoot SLA、人工取消与恢复链路验收。
- Modify: 对应的现有 `tests/test_*.py`、`tests/operations-dashboard-browser.py`。

---

### Task 1: Chatwoot 活动事件与 SLA 配置

**Files:**
- Modify: `app/chatwoot_events.py:9-99`
- Modify: `app/settings.py:8-115`
- Modify: `.env.example`
- Test: `tests/test_chatwoot_events.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `IncomingMessage.created_at: int`。
- Produces: `HumanReply(message_id, account_id, conversation_id, created_at)`。
- Produces: `parse_human_reply(payload, fallback_created_at=...) -> HumanReply | None`。
- Produces: `WorkerSettings.human_sla_seconds` 与 `human_fallback_cooldown_seconds`。
- Produces: `HandoffOperationsSettings.from_env()`，仅解析数据库、Chatwoot 和 SLA 所需配置。

- [ ] **Step 1: 为事件时间与人工公开回复编写失败测试**

```python
def test_parses_incoming_created_at_and_human_public_reply(self) -> None:
    incoming = incoming_payload()
    incoming["created_at"] = 1_720_958_390
    self.assertEqual(parse_incoming_message(incoming).created_at, 1_720_958_390)

    outgoing = incoming_payload()
    outgoing.update(
        {
            "id": 102,
            "message_type": "outgoing",
            "created_at": 1_720_958_400,
            "sender": {"id": 7, "type": "User"},
        }
    )
    self.assertEqual(
        parse_human_reply(outgoing, fallback_created_at=1_720_958_401),
        HumanReply(102, 1, 42, 1_720_958_400),
    )

def test_ignores_ai_private_bot_and_system_outgoing_messages(self) -> None:
    base = incoming_payload()
    base.update(
        {
            "message_type": "outgoing",
            "created_at": 1_720_958_400,
            "sender": {"id": 7, "type": "User"},
        }
    )
    variants = []
    for changes in (
        {"source_id": "ai-cs:101:reply"},
        {"private": True},
        {"sender": {"id": 7, "type": "AgentBot"}},
        {"event": "conversation_updated"},
        {"content_type": "input_select"},
    ):
        value = dict(base)
        value.update(changes)
        variants.append(value)
    for value in variants:
        self.assertIsNone(parse_human_reply(value, fallback_created_at=1_720_958_401))
```

- [ ] **Step 2: 运行事件测试确认失败**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_chatwoot_events -v`

Expected: FAIL，提示 `HumanReply` 或 `parse_human_reply` 尚不存在，或 `IncomingMessage` 没有 `created_at`。

- [ ] **Step 3: 实现规范化事件模型和单调事件时间**

```python
@dataclass(frozen=True)
class IncomingMessage:
    message_id: int
    account_id: int
    conversation_id: int
    sender_id: int
    content: str
    labels: tuple[str, ...]
    created_at: int = 0


@dataclass(frozen=True)
class HumanReply:
    message_id: int
    account_id: int
    conversation_id: int
    created_at: int


def parse_human_reply(
    payload: Mapping[str, Any],
    *,
    fallback_created_at: int,
) -> HumanReply | None:
    if payload.get("event") != "message_created":
        return None
    if payload.get("message_type") not in {"outgoing", 1}:
        return None
    if payload.get("private") is True or payload.get("is_private") is True:
        return None
    if payload.get("content_type") not in {None, "text"}:
        return None
    source_id = payload.get("source_id")
    if isinstance(source_id, str) and source_id.startswith("ai-cs:"):
        return None
    sender = payload.get("sender")
    if not isinstance(sender, Mapping):
        return None
    sender_type = sender.get("type")
    if not isinstance(sender_type, str) or sender_type.casefold() != "user":
        return None
    message_id = _positive_int(payload.get("id"))
    account_id = _positive_int(_mapping_id(payload.get("account")))
    conversation_id = _positive_int(_mapping_id(payload.get("conversation")))
    created_at = _event_timestamp(payload.get("created_at"), fallback_created_at)
    if None in {message_id, account_id, conversation_id, created_at}:
        return None
    return HumanReply(message_id, account_id, conversation_id, created_at)


def _mapping_id(value: object) -> object:
    return value.get("id") if isinstance(value, Mapping) else None


def _event_timestamp(value: object, fallback: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(fallback, int) and not isinstance(fallback, bool) and fallback >= 0:
        return fallback
    return None
```

`parse_incoming_message(payload, *, fallback_created_at: int = 0)` 同样调用 `_event_timestamp`；有效 Chatwoot `created_at` 优先，缺失时使用已通过签名时效校验的 Webhook 时间。对账路径始终传真实 `created_at`。`IncomingMessage.created_at=0` 只用于兼容现有直接构造的单元测试和内部调用；`SQLiteJobStore.enqueue` 遇到 0 时必须用本地入队时间替代，生产 Webhook 不会持久化 0。

- [ ] **Step 4: 为 SLA 与运维控制配置编写失败测试**

```python
def test_worker_settings_load_human_sla_defaults_and_overrides(self) -> None:
    defaults = WorkerSettings.from_env({})
    self.assertEqual(defaults.human_sla_seconds, 300)
    self.assertEqual(defaults.human_fallback_cooldown_seconds, 300)
    configured = WorkerSettings.from_env(
        {
            "CUSTOMER_SERVICE_HUMAN_SLA_SECONDS": "3",
            "CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS": "7",
        }
    )
    self.assertEqual(configured.human_sla_seconds, 3)
    self.assertEqual(configured.human_fallback_cooldown_seconds, 7)

def test_handoff_operations_settings_require_chatwoot_control_values(self) -> None:
    settings = HandoffOperationsSettings.from_env(
        {
            "CHATWOOT_BASE_URL": "http://127.0.0.1:3000/",
            "CHATWOOT_ACCOUNT_ID": "1",
            "CHATWOOT_API_TOKEN": "token",
            "CUSTOMER_SERVICE_DB_PATH": "state.db",
            "CUSTOMER_SERVICE_HUMAN_SLA_SECONDS": "300",
        }
    )
    self.assertEqual(settings.chatwoot_base_url, "http://127.0.0.1:3000")
    self.assertEqual(settings.human_sla_seconds, 300)
    self.assertNotIn("token", repr(settings))
```

- [ ] **Step 5: 实现并校验配置**

```python
@dataclass(frozen=True)
class HandoffOperationsSettings:
    chatwoot_base_url: str
    chatwoot_account_id: int
    chatwoot_api_token: str = field(repr=False)
    database_path: Path
    human_sla_seconds: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HandoffOperationsSettings":
        source = os.environ if environ is None else environ
        try:
            base_url = source.get("CHATWOOT_BASE_URL", "http://127.0.0.1:3000").strip().rstrip("/")
            account_id = int(source.get("CHATWOOT_ACCOUNT_ID", "").strip())
            token = source.get("CHATWOOT_API_TOKEN", "").strip()
            database_path = Path(source.get("CUSTOMER_SERVICE_DB_PATH", ".runtime/customer-service/state.db").strip())
            sla_seconds = int(source.get("CUSTOMER_SERVICE_HUMAN_SLA_SECONDS", "300").strip())
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Human handoff operations configuration is invalid.") from exc
        if not base_url or account_id <= 0 or not token or not str(database_path) or not 1 <= sla_seconds <= 86_400:
            raise ValueError("Human handoff operations configuration is invalid.")
        return cls(base_url, account_id, token, database_path, sla_seconds)
```

`WorkerSettings` 对两个新值均限制在 `1..86400`。在 `.env.example` 写入两个默认值且不写入任何真实密钥。

- [ ] **Step 6: 运行配置与事件测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_chatwoot_events tests.test_settings -v`

Expected: PASS。

- [ ] **Step 7: 提交任务 1**

```powershell
git add app/chatwoot_events.py app/settings.py .env.example tests/test_chatwoot_events.py tests/test_settings.py
git commit -m "feat: model human reply events and SLA settings"
```

---

### Task 2: SQLite 会话迁移与 `human_wait` 状态机

**Files:**
- Create: `app/handoff_schema.py`
- Modify: `app/conversation_store.py:1-147`
- Modify: `app/job_models.py:1-77`
- Modify: `app/job_store.py:1-589`
- Test: `tests/test_conversation_store.py`
- Test: `tests/test_job_models.py`
- Test: `tests/test_job_store.py`

**Interfaces:**
- Consumes: `IncomingMessage.created_at`、`HumanReply`、两个 Worker SLA 配置。
- Produces: `JobStage` 新值 `human_wait`。
- Produces: `JobKind = Literal["standard", "human_pending", "human_timeout_reply", "human_wait_notice"]`。
- Produces: `DurableJob.claimed_from_stage`, `message_created_at`, `received_at`, `job_kind`。
- Produces: `SQLiteJobStore.defer_human_wait(...) -> bool`、`confirm_human_fallback(...) -> bool`、`complete_without_delivery(...)`、`record_human_reply(...)`。
- Produces: `SQLiteConversationStore.save_handoff(message, reason, now=...)`。

- [ ] **Step 1: 编写向后兼容迁移和状态字段失败测试**

```python
def test_existing_conversation_schema_is_migrated_without_losing_state(self) -> None:
    with sqlite3.connect(self.database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE conversation_states (
                conversation_id INTEGER PRIMARY KEY,
                dify_conversation_id TEXT,
                mode TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO conversation_states VALUES (42, 'dify-old', 'human', CURRENT_TIMESTAMP);
            """
        )
    store = SQLiteConversationStore(self.database_path)
    state = store.get_conversation(42)
    self.assertEqual(state.mode, "human")
    self.assertIsNone(state.last_human_reply_at)

def test_save_handoff_records_reason_and_trigger_message(self) -> None:
    store = SQLiteConversationStore(self.database_path)
    message = IncomingMessage(101, 1, 42, 9, "我要人工", (), 1_000)
    store.save_handoff(message, "explicit_human_request", now=1_001)
    state = store.get_conversation(42)
    self.assertEqual(state.handoff_reason, "explicit_human_request")
    self.assertEqual(state.human_since, 1_001)
    self.assertEqual(state.last_customer_message_id, 101)
    self.assertEqual(state.last_customer_message_at, 1_000)
```

- [ ] **Step 2: 编写队列表迁移、等待领取和计数失败测试**

```python
def test_human_wait_is_durable_and_reclaim_does_not_increment_generation_attempts(self) -> None:
    store = SQLiteJobStore(self.database_path)
    store.enqueue(IncomingMessage(101, 1, 42, 9, "多久发货", (), 1_000), now=1_010)
    claimed = store.claim_next("worker-a", now=1_010, lease_seconds=300)
    self.assertEqual(claimed.generation_attempts, 1)
    self.assertTrue(store.defer_human_wait(claimed.id, available_at=1_310, now=1_011))
    reopened = SQLiteJobStore(self.database_path)
    self.assertIsNone(reopened.claim_next("worker-a", now=1_309, lease_seconds=300))
    due = reopened.claim_next("worker-a", now=1_310, lease_seconds=300)
    self.assertEqual(due.claimed_from_stage, "human_wait")
    self.assertEqual(due.generation_attempts, 1)
    self.assertEqual(due.job_kind, "human_pending")

def test_queue_metrics_count_human_wait_as_waiting(self) -> None:
    metrics = store.metrics(now=1_100)
    self.assertEqual(metrics.queued, 1)
    self.assertEqual(metrics.oldest_wait_seconds, 90)
```

- [ ] **Step 3: 实现共享会话迁移**

```python
CONVERSATION_COLUMNS = {
    "handoff_reason": "TEXT",
    "human_since": "INTEGER",
    "last_customer_message_id": "INTEGER",
    "last_customer_message_at": "INTEGER",
    "last_human_reply_message_id": "INTEGER",
    "last_human_reply_at": "INTEGER",
    "last_fallback_at": "INTEGER",
    "ai_resume_cutoff_message_id": "INTEGER",
}


def ensure_handoff_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_states (
            conversation_id INTEGER PRIMARY KEY,
            dify_conversation_id TEXT,
            mode TEXT NOT NULL CHECK(mode IN ('ai', 'human')),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    present = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(conversation_states)")
    }
    for name, declaration in CONVERSATION_COLUMNS.items():
        if name not in present:
            connection.execute(f"ALTER TABLE conversation_states ADD COLUMN {name} {declaration}")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_control_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('human_wait_notice','human_timeout_reply','ai_resumed')),
            result TEXT NOT NULL CHECK(result IN ('succeeded','failed')),
            error_code TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
```

`SQLiteConversationStore` 与 `SQLiteJobStore` 初始化时都调用 `ensure_handoff_schema(connection)`，因此 API 或 Worker 谁先启动都能得到完整会话表。

- [ ] **Step 4: 实现队列表幂等重建迁移**

在 `SQLiteJobStore._initialize()` 中，新建数据库直接使用以下约束；旧表的 `sqlite_master.sql` 不含 `'human_wait'` 时，在 `BEGIN IMMEDIATE` 中创建 `inbound_jobs_v2`、逐列复制、删除旧表、重命名并重建索引。复制时已有任务使用 `message_created_at=created_at`、`job_kind='standard'`。

```sql
stage TEXT NOT NULL CHECK(stage IN (
  'queued','generating','human_wait','ready_to_deliver','delivering',
  'retry_wait','completed','failed'
)),
job_kind TEXT NOT NULL DEFAULT 'standard' CHECK(job_kind IN (
  'standard','human_pending','human_timeout_reply','human_wait_notice'
)),
message_created_at INTEGER NOT NULL,
created_at INTEGER NOT NULL
```

旧表重建函数使用完整列映射，不依赖隐式列顺序：

```python
def _migrate_inbound_jobs_for_handoff(connection: sqlite3.Connection) -> None:
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='inbound_jobs'"
    ).fetchone()
    if table_sql_row is None:
        return
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(inbound_jobs)")
    }
    table_sql = str(table_sql_row[0] or "")
    if (
        "'human_wait'" in table_sql
        and "message_created_at" in columns
        and "job_kind" in columns
    ):
        return

    connection.execute("BEGIN IMMEDIATE")
    connection.execute("ALTER TABLE inbound_jobs RENAME TO inbound_jobs_v1")
    connection.execute(
        """
        CREATE TABLE inbound_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL UNIQUE,
            account_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            labels_json TEXT NOT NULL DEFAULT '[]',
            stage TEXT NOT NULL CHECK(stage IN (
                'queued','generating','human_wait','ready_to_deliver','delivering',
                'retry_wait','completed','failed'
            )),
            resume_stage TEXT CHECK(resume_stage IN ('generating','delivering')),
            decision TEXT CHECK(decision IN ('reply','handoff','ignore')),
            outcome TEXT,
            reply_text TEXT,
            dify_conversation_id TEXT,
            reason TEXT,
            record_knowledge_gap INTEGER NOT NULL DEFAULT 0
                CHECK(record_knowledge_gap IN (0,1)),
            outgoing_source_id TEXT NOT NULL,
            generation_attempts INTEGER NOT NULL DEFAULT 0,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            processing_duration_ms INTEGER NOT NULL DEFAULT 0,
            available_at INTEGER NOT NULL,
            locked_at INTEGER,
            locked_by TEXT,
            last_error_code TEXT,
            message_created_at INTEGER NOT NULL,
            job_kind TEXT NOT NULL DEFAULT 'standard' CHECK(job_kind IN (
                'standard','human_pending','human_timeout_reply','human_wait_notice'
            )),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO inbound_jobs (
            id, message_id, account_id, conversation_id, sender_id, content,
            labels_json, stage, resume_stage, decision, outcome, reply_text,
            dify_conversation_id, reason, record_knowledge_gap,
            outgoing_source_id, generation_attempts, delivery_attempts,
            processing_duration_ms, available_at, locked_at, locked_by,
            last_error_code, message_created_at, job_kind, created_at, updated_at
        )
        SELECT id, message_id, account_id, conversation_id, sender_id, content,
               labels_json, stage, resume_stage, decision, outcome, reply_text,
               dify_conversation_id, reason, record_knowledge_gap,
               outgoing_source_id, generation_attempts, delivery_attempts,
               processing_duration_ms, available_at, locked_at, locked_by,
               last_error_code, created_at, 'standard', created_at, updated_at
        FROM inbound_jobs_v1
        """
    )
    connection.execute("DROP TABLE inbound_jobs_v1")
    connection.execute(
        "CREATE INDEX idx_inbound_jobs_ready ON inbound_jobs(stage, available_at, id)"
    )
    connection.execute(
        "CREATE INDEX idx_inbound_jobs_conversation ON inbound_jobs(conversation_id, id, stage)"
    )
```

迁移测试先用当前旧约束创建一条 `queued` 和一条 `failed` 记录，再实例化新 Store，断言内容、错误码、尝试次数与状态全部保留，并能插入 `human_wait`。

- [ ] **Step 5: 实现领取来源、等待、取消和人工回复事务**

```python
def defer_human_wait(self, job_id: int, *, available_at: int, now: int) -> bool:
    with self._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT j.message_id, j.message_created_at, c.mode,
                   c.last_human_reply_at, c.last_human_reply_message_id
            FROM inbound_jobs AS j
            LEFT JOIN conversation_states AS c USING(conversation_id)
            WHERE j.id = ? AND j.stage = 'generating'
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} cannot make this transition.")
        replied = _event_not_before(
            row["last_human_reply_at"],
            row["last_human_reply_message_id"],
            row["message_created_at"],
            row["message_id"],
        )
        if row["mode"] != "human" or replied:
            self._complete_row(connection, job_id, now=now)
            return False
        connection.execute(
            """
            UPDATE inbound_jobs
            SET stage='human_wait', job_kind='human_pending',
                outgoing_source_id='ai-cs:' || message_id || ':human-fallback',
                available_at=?, locked_at=NULL, locked_by=NULL, updated_at=?
            WHERE id=? AND stage='generating'
            """,
            (available_at, now, job_id),
        )
        return True
```

`claim_next` 查询加入 `human_wait`，把它领取为 `generating`，同时把领取前状态写入返回对象的 `claimed_from_stage`；仅当 `job_kind='standard'` 且目标为 `generating` 时增加 `generation_attempts`。这样从 `human_wait` 领取以及该托底任务发生租约恢复时，都不会污染普通 Dify 生成尝试次数。

`record_human_reply(reply, now=...)` 在一个事务中：按 `(created_at, message_id)` 单调更新人工活动；把同会话、事件不晚于回复、`job_kind != 'standard'` 且阶段属于 `human_wait/generating/ready_to_deliver/retry_wait` 的任务完成；同步 upsert `processed_messages='completed'`。`delivering` 和 `failed` 不被静默取消。

- [ ] **Step 6: 实现托底资格与完成原子方法**

```python
def confirm_human_fallback(
    self,
    job_id: int,
    *,
    cooldown_seconds: int,
    now: int,
) -> bool:
    with self._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = self._fallback_state(connection, job_id)
        eligible = (
            row is not None
            and row["mode"] == "human"
            and not _event_not_before(
                row["last_human_reply_at"],
                row["last_human_reply_message_id"],
                row["message_created_at"],
                row["message_id"],
            )
            and (
                row["last_fallback_at"] is None
                or int(row["last_fallback_at"]) <= now - cooldown_seconds
            )
        )
        if not eligible:
            self._complete_row(connection, job_id, now=now)
        return eligible


def _fallback_state(
    self,
    connection: sqlite3.Connection,
    job_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT j.id, j.message_id, j.message_created_at, j.stage, j.job_kind,
               c.mode, c.last_human_reply_at, c.last_human_reply_message_id,
               c.last_fallback_at
        FROM inbound_jobs AS j
        JOIN conversation_states AS c USING(conversation_id)
        WHERE j.id=? AND j.job_kind != 'standard'
        """,
        (job_id,),
    ).fetchone()


def _event_not_before(
    candidate_at: int | None,
    candidate_id: int | None,
    target_at: int,
    target_id: int,
) -> bool:
    if candidate_at is None or candidate_id is None:
        return False
    return (int(candidate_at), int(candidate_id)) >= (target_at, target_id)


def _complete_row(
    self,
    connection: sqlite3.Connection,
    job_id: int,
    *,
    now: int,
) -> None:
    row = connection.execute(
        "SELECT message_id FROM inbound_jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Job {job_id} was not found.")
    connection.execute(
        """
        UPDATE inbound_jobs
        SET stage='completed', resume_stage=NULL, locked_at=NULL,
            locked_by=NULL, last_error_code=NULL, updated_at=?
        WHERE id=?
        """,
        (now, job_id),
    )
    connection.execute(
        """
        INSERT INTO processed_messages(message_id, status, updated_at)
        VALUES (?, 'completed', CURRENT_TIMESTAMP)
        ON CONFLICT(message_id) DO UPDATE SET
            status='completed', updated_at=CURRENT_TIMESTAMP
        """,
        (row["message_id"],),
    )
```

再实现：

- `save_human_fallback_prepared(job_id, action, job_kind, processing_duration_ms, now) -> bool`：事务中再次检查人工回复；成功时进入 `ready_to_deliver`，被取消时完成任务并返回 `False`。
- `can_deliver_human_fallback(job_id) -> bool`：投递前最后检查 mode 与人工回复，失败时完成未投递任务。
- `complete_human_fallback(job_id, now)`：仅允许 `delivering`；完成任务、写 `processed_messages`、更新 `last_fallback_at`，并按 `job_kind` 写成功控制事件。
- `complete_without_delivery(job_id, now)`：用于恢复 AI、人工已回复或冷却期命中后的无消息完成。

`save_human_fallback_prepared` 的更新条件必须同时包含 `stage='generating' AND job_kind='human_pending'`；`job_kind` 只接受两个托底结果值。`can_deliver_human_fallback` 只接受 `stage='delivering'`，用同一 `_event_not_before` 规则复核，失败时调用 `_complete_row`。这些条件使迟到的 Worker 不能覆盖人工回复事务已经完成的任务。

- [ ] **Step 7: 运行 SQLite 状态测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_conversation_store tests.test_job_models tests.test_job_store -v`

Expected: PASS，且旧表迁移测试确认已有数据未丢失。

- [ ] **Step 8: 提交任务 2**

```powershell
git add app/handoff_schema.py app/conversation_store.py app/job_models.py app/job_store.py tests/test_conversation_store.py tests/test_job_models.py tests/test_job_store.py
git commit -m "feat: persist human wait state and activity"
```

---

### Task 3: Webhook 人工回复、迟到消息和对账接入

**Files:**
- Modify: `app/chatwoot_webhook.py:1-61`
- Modify: `app/inbound_queue.py:1-30`
- Modify: `app/chatwoot_reconciliation.py:1-160`
- Modify: `app/main.py:130-179`
- Test: `tests/test_chatwoot_webhook.py`
- Test: `tests/test_inbound_queue.py`
- Test: `tests/test_chatwoot_reconciliation.py`

**Interfaces:**
- Consumes: `HumanReply` 与 `SQLiteJobStore.record_human_reply`。
- Produces: `WebhookEventSink.enqueue(...)` 与 `record_human_reply(...)`。
- Produces: Webhook 状态 `human_activity`；客户入队仍为 `queued/duplicate`。
- Guarantees: `message_id <= ai_resume_cutoff_message_id` 的迟到消息只幂等完成，不生成可执行任务。

- [ ] **Step 1: 编写人工出站 Webhook 和 AI 出站忽略测试**

```python
class FakeEventSink(FakeInboundQueue):
    def __init__(self) -> None:
        super().__init__()
        self.human_replies: list[HumanReply] = []

    def record_human_reply(self, reply: HumanReply) -> None:
        self.human_replies.append(reply)


def test_public_user_outgoing_records_human_activity(self) -> None:
    value = payload("outgoing")
    value.update({"created_at": 1_720_958_400, "sender": {"id": 7, "type": "User"}})
    raw_body, signature = encode_and_sign(value)
    result = self.process(raw_body, signature)
    self.assertEqual(result.status, "human_activity")
    self.assertEqual(self.queue.messages, [])
    self.assertEqual(self.queue.human_replies[0].conversation_id, 42)

def test_ai_outgoing_is_ignored_not_human_activity(self) -> None:
    value = payload("outgoing")
    value.update(
        {
            "created_at": 1_720_958_400,
            "sender": {"id": 7, "type": "User"},
            "source_id": "ai-cs:101:human-fallback",
        }
    )
    raw_body, signature = encode_and_sign(value)
    result = self.process(raw_body, signature)
    self.assertEqual(result.status, "ignored")
    self.assertEqual(self.queue.human_replies, [])
```

- [ ] **Step 2: 运行 Webhook 测试确认失败**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_chatwoot_webhook -v`

Expected: FAIL，现有处理器仍把所有 `outgoing` 事件忽略。

- [ ] **Step 3: 实现单一签名入口的事件分发**

```python
class WebhookEventSink(Protocol):
    def enqueue(self, message: IncomingMessage) -> EnqueueResult: ...
    def record_human_reply(self, reply: HumanReply) -> None: ...


accepted_at = now if now is not None else int(time.time())
incoming = parse_incoming_message(payload, fallback_created_at=accepted_at)
if incoming is not None and incoming.account_id == expected_account_id:
    result = event_sink.enqueue(incoming)
    return WebhookProcessingResult(status=result.status)
human_reply = parse_human_reply(payload, fallback_created_at=accepted_at)
if human_reply is not None and human_reply.account_id == expected_account_id:
    event_sink.record_human_reply(human_reply)
    return WebhookProcessingResult(status="human_activity")
return WebhookProcessingResult(status="ignored")
```

保持验签在 JSON 解析之前、256 KiB 限制和其他账户忽略规则不变。`main.py` 只在 `queued` 时返回 202，`human_activity` 返回 200。

- [ ] **Step 4: 让入队事务更新人工客户活动并拒绝恢复前旧消息**

在 `SQLiteJobStore.enqueue` 的 `BEGIN IMMEDIATE` 内，先读取会话：

```sql
SELECT mode, ai_resume_cutoff_message_id,
       last_customer_message_at, last_customer_message_id
FROM conversation_states
WHERE conversation_id = ?
```

若 `message_id <= ai_resume_cutoff_message_id`，upsert `processed_messages='completed'` 并返回 `duplicate`。若 mode 为 `human`，按 `(created_at, message_id)` 单调更新最近客户活动，然后插入任务；重复 Webhook 不得倒退字段。

`InboundQueue.record_human_reply` 只调用 Store，不记录客户正文或远程响应。

- [ ] **Step 5: 对账路径保留真实事件时间**

```python
return IncomingMessage(
    message_id=message_id,
    account_id=self._account_id,
    conversation_id=conversation.id,
    sender_id=sender_id,
    content=content.strip(),
    labels=conversation.labels,
    created_at=created_at,
)
```

增加测试：恢复截止为 600 时，对账回补 ID 599 返回 `duplicate` 且没有队列任务；ID 601 正常入队。

- [ ] **Step 6: 运行 Webhook、入队与对账测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_chatwoot_webhook tests.test_inbound_queue tests.test_chatwoot_reconciliation -v`

Expected: PASS；人工出站路径不构造 Dify 或发起 Chatwoot API 请求。

- [ ] **Step 7: 提交任务 3**

```powershell
git add app/chatwoot_webhook.py app/inbound_queue.py app/chatwoot_reconciliation.py app/main.py tests/test_chatwoot_webhook.py tests/test_inbound_queue.py tests/test_chatwoot_reconciliation.py
git commit -m "feat: capture human replies from Chatwoot webhooks"
```

---

### Task 4: Worker 超时安全托底与人工优先取消

**Files:**
- Modify: `app/job_models.py`
- Modify: `app/customer_service.py:36-287`
- Modify: `app/job_processor.py:1-184`
- Modify: `app/worker.py:127-198`
- Test: `tests/test_customer_service.py`
- Test: `tests/test_job_processor.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `DurableJob.claimed_from_stage/job_kind` 与 Store 的托底事务方法。
- Produces: `HumanFallbackPreparation(action, job_kind)`。
- Produces: `CustomerService.prepare_human_fallback(message)` 与 `deliver_human_fallback(...)`。
- Guarantees: 托底投递不调用 `save_conversation(... mode="ai")`，会话持续为 `human`。

- [ ] **Step 1: 编写安全分类和客户文案失败测试**

```python
def test_high_risk_human_timeout_returns_wait_notice_without_dify(self) -> None:
    service = self.make_service()
    result = service.prepare_human_fallback(self.message(content="我要投诉并申请赔偿"))
    self.assertEqual(result.job_kind, "human_wait_notice")
    self.assertIn("人工客服", result.action.customer_text)
    self.assertEqual(self.dify.calls, [])

def test_grounded_human_timeout_returns_temporary_ai_reply(self) -> None:
    self.dify.response = DifyChatResponse("现货商品 48 小时内发出。", "dify-2")
    result = self.make_service().prepare_human_fallback(self.message(content="多久发货"))
    self.assertEqual(result.job_kind, "human_timeout_reply")
    self.assertTrue(result.action.customer_text.startswith("AI 临时协助："))

def test_knowledge_gap_and_dify_error_use_wait_notice(self) -> None:
    self.dify.response = DifyChatResponse("[HANDOFF] 暂无可靠资料。", "dify-2")
    gap = self.make_service().prepare_human_fallback(self.message(content="未知政策"))
    self.assertEqual(gap.job_kind, "human_wait_notice")
    self.assertTrue(gap.action.record_knowledge_gap)
```

- [ ] **Step 2: 实现托底准备类型与固定文案**

```python
@dataclass(frozen=True)
class HumanFallbackPreparation:
    action: PreparedAction
    job_kind: Literal["human_timeout_reply", "human_wait_notice"]
    dify_used: bool


_HUMAN_WAIT_TEXT = (
    "人工客服暂时未能及时回复，我们已提醒工作人员，请稍候。"
    "如涉及支付、账户或投诉问题，请不要发送密码、验证码等敏感信息。"
)


def prepare_human_fallback(self, message: IncomingMessage) -> HumanFallbackPreparation:
    if handoff_reason(message.content) is not None:
        return HumanFallbackPreparation(
            PreparedAction.customer_message(_HUMAN_WAIT_TEXT),
            "human_wait_notice",
            False,
        )
    response = self._dify.send_message(
        query=message.content,
        user=f"chatwoot:{message.sender_id}",
        conversation_id=None,
    )
    parsed = parse_dify_answer(response.answer)
    if parsed.needs_human:
        return HumanFallbackPreparation(
            PreparedAction.customer_message(
                _HUMAN_WAIT_TEXT,
                record_knowledge_gap=response.answer.strip().upper().startswith("[HANDOFF]"),
            ),
            "human_wait_notice",
            True,
        )
    text = (
        f"AI 临时协助：{parsed.customer_text}\n\n"
        "人工客服仍在接管本会话，如需进一步处理请稍候。"
    )
    return HumanFallbackPreparation(
        PreparedAction.customer_message(text),
        "human_timeout_reply",
        True,
    )
```

`PreparedAction.customer_message` 使用 `decision='reply'`、`outcome='ignored_human_mode'`、`dify_conversation_id=None`；该 action 只允许走托底投递分支，不调用普通 `record_result`：

```python
@classmethod
def customer_message(
    cls,
    text: str,
    *,
    record_knowledge_gap: bool = False,
) -> "PreparedAction":
    normalized = text.strip()
    if not normalized:
        raise ValueError("Customer-visible action text is required.")
    return cls(
        "reply",
        "ignored_human_mode",
        normalized,
        None,
        None,
        record_knowledge_gap,
    )
```

- [ ] **Step 3: 编写 JobProcessor 等待、取消、冷却和投递失败测试**

```python
def test_human_mode_generation_is_deferred_until_sla(self) -> None:
    job = durable_job(stage="generating", claimed_from_stage="queued", received_at=1_000)
    self.service.action = PreparedAction.ignore_human_mode()
    self.processor.process(job)
    self.assertEqual(self.store.deferred, [(job.id, 1_300, 1_010)])

def test_due_human_wait_prepares_fallback_without_incrementing_attempt(self) -> None:
    job = durable_job(
        stage="generating",
        claimed_from_stage="human_wait",
        job_kind="human_pending",
        generation_attempts=1,
    )
    self.processor.process(job)
    self.assertEqual(self.store.saved_fallback[0].job_kind, "human_timeout_reply")

def test_expired_human_fallback_lease_resumes_same_fallback_path(self) -> None:
    job = durable_job(
        stage="generating",
        claimed_from_stage="retry_wait",
        job_kind="human_pending",
        generation_attempts=1,
    )
    self.processor.process(job)
    self.assertEqual(self.service.standard_prepare_calls, 0)
    self.assertEqual(self.service.fallback_prepare_calls, 1)

def test_human_reply_before_delivery_completes_without_sending(self) -> None:
    job = durable_job(stage="delivering", job_kind="human_timeout_reply")
    self.store.can_deliver = False
    self.processor.process(job)
    self.assertEqual(self.chatwoot.sent, [])
    self.assertEqual(self.store.completed_without_delivery, [job.id])
```

- [ ] **Step 4: 实现 JobProcessor 两条生成路径**

```python
def _process_generation(self, job: DurableJob) -> None:
    if job.job_kind == "human_pending":
        self._process_human_fallback_generation(job)
        return
    message = _incoming_message(job)
    started_at = self._monotonic()
    try:
        action = self._service.prepare(message)
    except DifyChatAPIError as exc:
        duration_ms = _duration_ms(started_at, self._monotonic())
        now = self._clock()
        error_code = _dify_error_code(exc)
        self._store.record_dependency_status(
            "dify", ok=False, error_code=error_code, checked_at=now
        )
        if exc.status_code in {401, 403} or (
            job.generation_attempts >= self._settings.generation_max_attempts
        ):
            self._store.save_prepared(
                job.id,
                self._service.dify_error_action(),
                processing_duration_ms=duration_ms,
                now=now,
            )
            return
        self._store.schedule_retry(
            job.id,
            resume_stage="generating",
            error_code=error_code,
            available_at=now + retry_delay_seconds(job.generation_attempts),
            now=now,
        )
        return
    if action.decision == "ignore":
        self._store.defer_human_wait(
            job.id,
            available_at=job.received_at + self._settings.human_sla_seconds,
            now=self._clock(),
        )
        return
    duration_ms = _duration_ms(started_at, self._monotonic())
    now = self._clock()
    self._store.save_prepared(
        job.id,
        action,
        processing_duration_ms=duration_ms,
        now=now,
    )
    self._store.record_dependency_status(
        "dify", ok=True, error_code=None, checked_at=now
    )
```

`_process_human_fallback_generation` 的顺序固定为：

1. `confirm_human_fallback`；返回 `False` 时结束。
2. 调用 `prepare_human_fallback`。
3. Dify 异常不重试业务回答，转成 `human_wait_notice`，并记录脱敏依赖错误码。
4. 成功 Dify 调用记录健康状态。
5. 调用 `save_human_fallback_prepared`；若期间人工已回复，返回 `False` 且不进入投递。

```python
def _process_human_fallback_generation(self, job: DurableJob) -> None:
    now = self._clock()
    if not self._store.confirm_human_fallback(
        job.id,
        cooldown_seconds=self._settings.human_fallback_cooldown_seconds,
        now=now,
    ):
        return
    message = _incoming_message(job)
    started_at = self._monotonic()
    try:
        prepared = self._service.prepare_human_fallback(message)
    except DifyChatAPIError as exc:
        error_code = _dify_error_code(exc)
        self._store.record_dependency_status(
            "dify", ok=False, error_code=error_code, checked_at=now
        )
        prepared = HumanFallbackPreparation(
            PreparedAction.customer_message(self._service.human_wait_text()),
            "human_wait_notice",
            False,
        )
    else:
        if prepared.dify_used:
            self._store.record_dependency_status(
                "dify", ok=True, error_code=None, checked_at=now
            )
    self._store.save_human_fallback_prepared(
        job.id,
        prepared.action,
        job_kind=prepared.job_kind,
        processing_duration_ms=_duration_ms(started_at, self._monotonic()),
        now=self._clock(),
    )
```

`CustomerService.human_wait_text() -> str` 返回固定 `_HUMAN_WAIT_TEXT`，避免 JobProcessor 复制客户文案。

- [ ] **Step 5: 实现不改变模式的幂等托底投递**

```python
def deliver_human_fallback(
    self,
    message: IncomingMessage,
    action: PreparedAction,
    *,
    source_id: str,
) -> None:
    if action.customer_text is None:
        raise ValueError("Human fallback text is required.")
    existing = self._chatwoot.find_outgoing_message_by_source_id(
        message.account_id, message.conversation_id, source_id
    )
    if existing is None:
        self._chatwoot.send_outgoing_message(
            message.account_id,
            message.conversation_id,
            action.customer_text,
            source_id=source_id,
        )
```

投递分支先调用 `can_deliver_human_fallback`，成功后调用上面的方法和 `complete_human_fallback`。Chatwoot 可重试错误沿用当前退避；认证失败或达到次数上限进入 `failed`，写对应失败控制事件但不改变会话模式。

```python
def _process_delivery(self, job: DurableJob) -> None:
    if job.action is None:
        raise ValueError(f"Job {job.id} has no prepared action.")
    if job.job_kind != "standard":
        if not self._store.can_deliver_human_fallback(job.id, now=self._clock()):
            return
        try:
            self._service.deliver_human_fallback(
                _incoming_message(job),
                job.action,
                source_id=job.outgoing_source_id,
            )
        except ChatwootAPIError as exc:
            self._handle_delivery_error(job, exc, _chatwoot_error_code(exc))
            return
        self._store.complete_human_fallback(job.id, now=self._clock())
        self._store.record_dependency_status(
            "chatwoot", ok=True, error_code=None, checked_at=self._clock()
        )
        return
    self._process_standard_delivery(job)
```

把现有普通投递函数体原样移入 `_process_standard_delivery(job)`；该拆分不改变普通回答、转人工标签、统计记录或重试行为。

- [ ] **Step 6: 记录转人工原因和触发消息**

普通 `CustomerService.deliver` 的 handoff 分支在标签成功后调用：

```python
self._store.save_handoff(
    message,
    cast(Reason, action.reason),
    now=int(time.time()),
)
```

AI 正常回答仍保存 Dify 会话 ID 与 `mode='ai'`；托底分支不保存 Dify 会话 ID。

- [ ] **Step 7: 运行 Worker 业务测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_customer_service tests.test_job_processor tests.test_worker -v`

Expected: PASS；测试明确断言人工回复取消、冷却期无重复、Dify 故障只有等待提示、会话 mode 仍为 `human`。

- [ ] **Step 8: 提交任务 4**

```powershell
git add app/job_models.py app/customer_service.py app/job_processor.py app/worker.py tests/test_customer_service.py tests/test_job_processor.py tests/test_worker.py
git commit -m "feat: add safe AI fallback for unattended handoffs"
```

---

### Task 5: Chatwoot 恢复控制与 SQLite 原子恢复

**Files:**
- Create: `app/handoff_control.py`
- Modify: `app/chatwoot_client.py:1-277`
- Modify: `app/job_store.py`
- Test: `tests/test_chatwoot_client.py`
- Create: `tests/test_handoff_control.py`
- Test: `tests/test_job_store.py`

**Interfaces:**
- Produces: `ChatwootClient.latest_message_id(account_id, conversation_id) -> int`。
- Produces: `ChatwootClient.remove_label_preserving_existing(..., label) -> tuple[str, ...]`。
- Produces: `HumanConversation`、`ResumeResult(status: Literal["resumed", "already_ai"])`。
- Produces: `HandoffControlService.resume_ai(conversation_id) -> ResumeResult`。
- Produces: `SQLiteJobStore.resume_ai_transaction(...) -> ResumeStoreStatus`，其中 `ResumeStoreStatus = Literal["resumed", "already_ai", "not_found", "conflict"]`。

- [ ] **Step 1: 为标签保留和截止消息 ID 编写失败测试**

```python
def test_remove_label_preserves_unrelated_labels(self) -> None:
    responses = iter(
        (
            FakeResponse({"payload": ["vip", "ai_handoff", "售后"]}),
            FakeResponse({"payload": ["vip", "售后"]}),
        )
    )
    client = ChatwootClient("http://chatwoot", "token", opener=lambda *args, **kwargs: next(responses))
    labels = client.remove_label_preserving_existing(1, 42, "ai_handoff")
    self.assertEqual(labels, ("vip", "售后"))

def test_latest_message_id_returns_maximum_valid_id(self) -> None:
    client = self.client_returning_messages([{"id": 9}, {"id": 12}, {"id": "bad"}])
    self.assertEqual(client.latest_message_id(1, 42), 12)
```

- [ ] **Step 2: 实现 Chatwoot 控制方法**

```python
def latest_message_id(self, account_id: int, conversation_id: int) -> int:
    messages = self.list_conversation_messages(account_id, conversation_id)
    ids = [message["id"] for message in messages if _is_positive_int(message.get("id"))]
    if not ids:
        raise ChatwootAPIError(None, "Chatwoot returned no conversation message ID.")
    return max(ids)

def remove_label_preserving_existing(
    self, account_id: int, conversation_id: int, label: str
) -> tuple[str, ...]:
    normalized = label.strip()
    if not normalized:
        raise ValueError("Conversation label is required.")
    current = self.get_labels(account_id, conversation_id)
    labels = tuple(item for item in current if item != normalized)
    if labels != current:
        path = f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
        self._request_json("POST", path, {"labels": list(labels)})
    return labels
```

- [ ] **Step 3: 编写恢复事务的存在、幂等、冲突和旧任务测试**

```python
def test_resume_transaction_cancels_only_safe_old_jobs_and_sets_cutoff(self) -> None:
    self.seed_human_conversation(42)
    queued = self.enqueue_job(101, 42, "queued")
    waiting = self.enqueue_job(102, 42, "human_wait")
    status = self.store.resume_ai_transaction(42, cutoff_message_id=900, now=2_000)
    self.assertEqual(status, "resumed")
    self.assertEqual(self.store.get_conversation_mode(42), "ai")
    self.assertEqual(self.store.get_job(queued).stage, "completed")
    self.assertEqual(self.store.get_job(waiting).stage, "completed")
    self.assertEqual(self.store.get_resume_cutoff(42), 900)

def test_resume_transaction_rejects_active_retry_and_failed_jobs(self) -> None:
    for stage in ("generating", "ready_to_deliver", "delivering", "retry_wait", "failed"):
        with self.subTest(stage=stage):
            self.seed_human_conversation_with_job(stage)
            self.assertEqual(
                self.store.resume_ai_transaction(42, cutoff_message_id=900, now=2_000),
                "conflict",
            )
            self.assertEqual(self.store.get_conversation_mode(42), "human")
```

- [ ] **Step 4: 实现 `BEGIN IMMEDIATE` 恢复事务**

事务内按以下顺序执行，所有 SQL 使用同一连接：

```python
connection.execute("BEGIN IMMEDIATE")
state = connection.execute(
    "SELECT mode FROM conversation_states WHERE conversation_id=?",
    (conversation_id,),
).fetchone()
if state is None:
    return "not_found"
if state["mode"] == "ai":
    return "already_ai"
blocking = connection.execute(
    """
    SELECT 1 FROM inbound_jobs
    WHERE conversation_id=?
      AND stage IN ('generating','ready_to_deliver','delivering','retry_wait','failed')
    LIMIT 1
    """,
    (conversation_id,),
).fetchone()
if blocking is not None:
    return "conflict"
self._complete_safe_jobs(connection, conversation_id, now=now)
connection.execute(
    """
    UPDATE conversation_states
    SET mode='ai', dify_conversation_id=NULL, handoff_reason=NULL,
        human_since=NULL, last_customer_message_id=NULL,
        last_customer_message_at=NULL, last_human_reply_message_id=NULL,
        last_human_reply_at=NULL, last_fallback_at=NULL,
        ai_resume_cutoff_message_id=?, updated_at=CURRENT_TIMESTAMP
    WHERE conversation_id=? AND mode='human'
    """,
    (cutoff_message_id, conversation_id),
)
connection.execute(
    """
    INSERT INTO conversation_control_events
      (conversation_id, action, result, error_code, created_at)
    VALUES (?, 'ai_resumed', 'succeeded', NULL, ?)
    """,
    (conversation_id, now),
)
```

`_complete_safe_jobs` 只处理 `queued/human_wait` 并同步 `processed_messages`。另提供只读 `get_resume_precheck`，在远程调用前尽早返回不存在、已是 AI 或冲突。

```python
def get_resume_precheck(self, conversation_id: int) -> ResumeStoreStatus | Literal["ready"]:
    with self._connect() as connection:
        state = connection.execute(
            "SELECT mode FROM conversation_states WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if state is None:
            return "not_found"
        if state["mode"] == "ai":
            return "already_ai"
        blocking = connection.execute(
            """
            SELECT 1 FROM inbound_jobs
            WHERE conversation_id=?
              AND stage IN ('generating','ready_to_deliver','delivering','retry_wait','failed')
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        return "conflict" if blocking is not None else "ready"


def _complete_safe_jobs(
    self,
    connection: sqlite3.Connection,
    conversation_id: int,
    *,
    now: int,
) -> None:
    rows = connection.execute(
        """
        SELECT id, message_id FROM inbound_jobs
        WHERE conversation_id=? AND stage IN ('queued','human_wait')
        """,
        (conversation_id,),
    ).fetchall()
    for row in rows:
        self._complete_row(connection, int(row["id"]), now=now)


def record_control_failure(
    self,
    conversation_id: int,
    action: Literal["ai_resumed"],
    error_code: str,
    *,
    now: int,
) -> None:
    self._validate_error_code(error_code)
    with self._connect() as connection:
        connection.execute(
            """
            INSERT INTO conversation_control_events
              (conversation_id, action, result, error_code, created_at)
            VALUES (?, ?, 'failed', ?, ?)
            """,
            (conversation_id, action, error_code, now),
        )
```

- [ ] **Step 5: 编写控制服务远程失败和事务二次冲突测试**

```python
def test_chatwoot_failure_keeps_local_mode_human(self) -> None:
    self.chatwoot.latest_error = ChatwootAPIError(503, "unavailable")
    with self.assertRaises(HandoffRemoteError) as raised:
        self.service.resume_ai(42)
    self.assertEqual(raised.exception.error_code, "chatwoot_server_error")
    self.assertEqual(self.store.mode, "human")

def test_local_conflict_after_label_removal_is_fail_safe(self) -> None:
    self.store.precheck = "ready"
    self.store.resume_status = "conflict"
    with self.assertRaises(HandoffConflictError):
        self.service.resume_ai(42)
    self.assertTrue(self.chatwoot.label_removed)
    self.assertEqual(self.store.mode, "human")

def test_retry_after_label_was_already_removed_finishes_local_resume(self) -> None:
    self.chatwoot.labels = ("vip",)
    self.store.precheck = "ready"
    self.store.resume_status = "resumed"
    result = self.service.resume_ai(42)
    self.assertEqual(result.status, "resumed")
    self.assertEqual(self.chatwoot.posted_label_updates, 0)
    self.assertEqual(self.store.mode, "ai")

def test_already_ai_is_idempotent_without_new_audit_event(self) -> None:
    self.store.precheck = "already_ai"
    before = self.store.control_event_count
    result = self.service.resume_ai(42)
    self.assertEqual(result.status, "already_ai")
    self.assertEqual(self.store.control_event_count, before)
    self.assertEqual(self.chatwoot.calls, [])
```

- [ ] **Step 6: 实现恢复编排和稳定错误码**

```python
ResumeStoreStatus = Literal["resumed", "already_ai", "not_found", "conflict"]


@dataclass(frozen=True)
class ResumeResult:
    status: Literal["resumed", "already_ai"]


class HandoffNotFoundError(RuntimeError):
    pass


class HandoffConflictError(RuntimeError):
    pass


class HandoffRemoteError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class HandoffStorageError(RuntimeError):
    pass


def chatwoot_control_error_code(error: ChatwootAPIError) -> str:
    if error.status_code in {401, 403}:
        return "chatwoot_auth_error"
    if error.status_code == 429:
        return "chatwoot_rate_limited"
    if error.status_code is not None and error.status_code >= 500:
        return "chatwoot_server_error"
    return "chatwoot_timeout" if error.status_code is None else "chatwoot_request_error"


SlaStatus = Literal["normal", "due_soon", "overdue"]


@dataclass(frozen=True)
class HumanConversation:
    conversation_id: int
    handoff_reason: str | None
    human_since: int | None
    last_customer_message_at: int | None
    last_human_reply_at: int | None
    last_fallback_at: int | None
    waiting_seconds: int
    sla_status: SlaStatus
    has_blocking_job: bool
    chatwoot_url: str


class HandoffControlService:
    def __init__(
        self,
        *,
        store: SQLiteJobStore,
        chatwoot: ChatwootClient,
        account_id: int,
        base_url: str,
        human_sla_seconds: int,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._chatwoot = chatwoot
        self._account_id = account_id
        self._base_url = base_url.rstrip("/")
        self._human_sla_seconds = human_sla_seconds
        self._clock = clock or (lambda: int(time.time()))

    def resume_ai(self, conversation_id: int) -> ResumeResult:
        precheck = self._store.get_resume_precheck(conversation_id)
        if precheck == "not_found":
            raise HandoffNotFoundError()
        if precheck == "already_ai":
            return ResumeResult("already_ai")
        if precheck == "conflict":
            raise HandoffConflictError()
        try:
            cutoff = self._chatwoot.latest_message_id(self._account_id, conversation_id)
            self._chatwoot.remove_label_preserving_existing(
                self._account_id, conversation_id, "ai_handoff"
            )
        except ChatwootAPIError as exc:
            code = chatwoot_control_error_code(exc)
            self._store.record_control_failure(conversation_id, "ai_resumed", code, now=self._clock())
            raise HandoffRemoteError(code) from exc
        status = self._store.resume_ai_transaction(
            conversation_id,
            cutoff_message_id=cutoff,
            now=self._clock(),
        )
        if status == "conflict":
            raise HandoffConflictError()
        if status == "not_found":
            raise HandoffNotFoundError()
        return ResumeResult(status)
```

构造服务时不得打开长生命周期 SQLite 连接。`get_resume_precheck`、`record_control_failure` 或 `resume_ai_transaction` 抛出 `sqlite3.Error` 时，控制服务统一抛出不含原错误正文的 `HandoffStorageError`；SQLite 自身负责回滚，mode 保持 `human`。

远程错误码限定为 `chatwoot_auth_error`、`chatwoot_rate_limited`、`chatwoot_server_error`、`chatwoot_timeout`、`chatwoot_request_error`，不保存异常正文。

- [ ] **Step 7: 运行 Chatwoot、Store 与控制服务测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_chatwoot_client tests.test_job_store tests.test_handoff_control -v`

Expected: PASS。

- [ ] **Step 8: 提交任务 5**

```powershell
git add app/handoff_control.py app/chatwoot_client.py app/job_store.py tests/test_chatwoot_client.py tests/test_job_store.py tests/test_handoff_control.py
git commit -m "feat: safely resume AI ownership"
```

---

### Task 6: 受保护的人工会话运维 API

**Files:**
- Modify: `app/handoff_control.py`
- Modify: `app/operations_api.py:1-163`
- Test: `tests/test_operations_api.py`

**Interfaces:**
- Consumes: `HandoffOperationsSettings`、`HandoffControlService`、`OPERATIONS_API_KEY`。
- Produces: `GET /v1/operations/human-conversations`。
- Produces: `POST /v1/operations/conversations/{conversation_id}/resume-ai`。
- Guarantees: 列表无正文和 PII；状态为 `normal/due_soon/overdue`；错误映射为 401/404/409/502/503。

- [ ] **Step 1: 编写列表隐私、SLA 和认证失败测试**

```python
def test_human_conversation_list_is_protected_private_and_classified(self) -> None:
    headers = self.configure_key()
    self.seed_human_conversation(
        conversation_id=42,
        human_since=1_000,
        last_customer_message_at=1_100,
        last_human_reply_at=None,
    )
    unauthorized = self.client.get("/v1/operations/human-conversations")
    with patch("app.operations_api.current_timestamp", return_value=1_400):
        response = self.client.get(
            "/v1/operations/human-conversations", headers=headers
        )
    self.assertEqual(unauthorized.status_code, 401)
    self.assertEqual(response.status_code, 200)
    item = response.json()["items"][0]
    self.assertEqual(item["sla_status"], "overdue")
    self.assertEqual(item["waiting_seconds"], 300)
    self.assertNotIn("content", response.text)
    self.assertNotIn("private customer question", response.text)
```

再加入边界断言：等待 239 秒为 `normal`，240..299 秒为 `due_soon`，300 秒起为 `overdue`；人工回复时间不早于客户消息时 waiting 为 0、状态 normal；`last_fallback_at` 不会清除 overdue。

- [ ] **Step 2: 实现脱敏人工会话查询**

Store SQL 只选择以下列和阻塞任务布尔值：

```sql
SELECT c.conversation_id, c.handoff_reason, c.human_since,
       c.last_customer_message_id, c.last_customer_message_at,
       c.last_human_reply_message_id, c.last_human_reply_at,
       c.last_fallback_at,
       EXISTS (
         SELECT 1 FROM inbound_jobs j
         WHERE j.conversation_id=c.conversation_id
           AND j.stage IN ('generating','ready_to_deliver','delivering','retry_wait','failed')
       ) AS has_blocking_job
FROM conversation_states c
WHERE c.mode='human'
ORDER BY COALESCE(c.last_customer_message_at, c.human_since) ASC,
         c.conversation_id ASC
```

`HandoffControlService.list_human_conversations()` 使用构造函数注入的 `clock` 与 SLA 计算：

```python
ratio = waiting_seconds / sla_seconds if sla_seconds else 1
sla_status = "overdue" if ratio >= 1 else "due_soon" if ratio >= 0.8 else "normal"
chatwoot_url = (
    f"{base_url}/app/accounts/{account_id}/conversations/{conversation_id}"
)
```

方法签名固定为 `list_human_conversations(self) -> tuple[HumanConversation, ...]`；它使用构造函数注入的时钟、SLA、Chatwoot base URL 和 account ID，不从每行或请求参数读取这些值。是否已获人工回复使用 `(last_human_reply_at, last_human_reply_message_id) >= (last_customer_message_at, last_customer_message_id)` 判断；两个消息 ID 只用于服务内部排序，不进入 API 返回模型。

- [ ] **Step 3: 编写恢复 API 状态映射测试**

```python
def test_resume_api_maps_success_idempotency_and_failures(self) -> None:
    headers = self.configure_key()
    cases = (
        (ResumeResult("resumed"), None, 200, "resumed"),
        (ResumeResult("already_ai"), None, 200, "already_ai"),
        (None, HandoffNotFoundError(), 404, None),
        (None, HandoffConflictError(), 409, None),
        (None, HandoffRemoteError("chatwoot_server_error"), 502, None),
        (None, HandoffStorageError(), 503, None),
    )
    for result, error, expected_status, expected_body in cases:
        with self.subTest(expected_status=expected_status), patch(
            "app.operations_api.get_handoff_control_service"
        ) as factory:
            if error is None:
                factory.return_value.resume_ai.return_value = result
            else:
                factory.return_value.resume_ai.side_effect = error
            response = self.client.post(
                "/v1/operations/conversations/42/resume-ai", headers=headers
            )
            self.assertEqual(response.status_code, expected_status)
            if expected_body:
                self.assertEqual(response.json()["status"], expected_body)
```

- [ ] **Step 4: 实现运维依赖和两个路由**

```python
def require_handoff_operations_settings(
    operations: OperationsDependency,
) -> HandoffOperationsSettings:
    try:
        return HandoffOperationsSettings.from_env()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="Human handoff operations are not configured.",
        ) from exc


def get_handoff_control_service(
    settings: HandoffOperationsSettings,
) -> HandoffControlService:
    return HandoffControlService(
        store=SQLiteJobStore(settings.database_path),
        chatwoot=ChatwootClient(
            base_url=settings.chatwoot_base_url,
            api_token=settings.chatwoot_api_token,
            timeout=15,
        ),
        account_id=settings.chatwoot_account_id,
        base_url=settings.chatwoot_base_url,
        human_sla_seconds=settings.human_sla_seconds,
        clock=current_timestamp,
    )


@operations_router.get("/human-conversations")
def list_human_conversations(
    settings: Annotated[HandoffOperationsSettings, Depends(require_handoff_operations_settings)],
) -> dict[str, object]:
    service = get_handoff_control_service(settings)
    return {"items": [asdict(item) for item in service.list_human_conversations()]}


@operations_router.post("/conversations/{conversation_id}/resume-ai")
def resume_ai_conversation(
    conversation_id: Annotated[int, Path(gt=0)],
    settings: Annotated[HandoffOperationsSettings, Depends(require_handoff_operations_settings)],
) -> dict[str, str]:
    try:
        return asdict(get_handoff_control_service(settings).resume_ai(conversation_id))
    except HandoffNotFoundError as exc:
        raise HTTPException(404, "Human conversation not found.") from exc
    except HandoffConflictError as exc:
        raise HTTPException(409, "Conversation has a blocking job.") from exc
    except HandoffRemoteError as exc:
        raise HTTPException(502, f"Chatwoot control failed: {exc.error_code}.") from exc
    except HandoffStorageError as exc:
        raise HTTPException(503, "Human handoff state is temporarily unavailable.") from exc
```

依赖顺序必须先通过 `X-Operations-Key`，再解析 Chatwoot 控制配置或构造客户端。响应中不返回 token、标签全文或远程错误正文。

- [ ] **Step 5: 运行运维 API 测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_api tests.test_handoff_control -v`

Expected: PASS。

- [ ] **Step 6: 提交任务 6**

```powershell
git add app/handoff_control.py app/operations_api.py tests/test_operations_api.py
git commit -m "feat: expose protected human handoff controls"
```

---

### Task 7: 运维看板人工会话与恢复确认

**Files:**
- Modify: `app/static/operations.html:1-769`
- Modify: `tests/test_operations_dashboard.py`
- Modify: `tests/operations-dashboard-browser.py`

**Interfaces:**
- Consumes: 人工会话 GET 与恢复 POST API。
- Produces: `#humanConversationList`、`#humanConversationCount`、`#resumeDialog`、`#resumeConfirmButton`。
- Guarantees: 二次确认、逐行禁用、成功移除、失败可重试、360px 无水平溢出、不使用不安全 HTML。

- [ ] **Step 1: 编写静态结构和安全渲染失败测试**

```python
def test_dashboard_contains_human_handoff_controls(self) -> None:
    source = Path("app/static/operations.html").read_text(encoding="utf-8")
    for element_id in (
        "humanConversationList",
        "humanConversationCount",
        "resumeDialog",
        "resumeConversationId",
        "resumeCancelButton",
        "resumeConfirmButton",
    ):
        self.assertIn(f'id="{element_id}"', source)
    self.assertIn("/v1/operations/human-conversations", source)
    self.assertIn("/resume-ai", source)
    self.assertNotIn("window.confirm", source)
    self.assertNotIn(".innerHTML", source)
```

- [ ] **Step 2: 添加人工接管区域和确认弹窗**

```html
<section class="card human-panel" aria-labelledby="humanConversationTitle">
  <div class="panel-header">
    <div>
      <h2 id="humanConversationTitle" class="panel-title">人工接管会话</h2>
      <p id="humanConversationCount" class="panel-subtitle">等待加载</p>
    </div>
  </div>
  <div id="humanConversationList" class="human-list" aria-live="polite"></div>
</section>

<dialog id="resumeDialog">
  <form method="dialog" class="dialog-content">
    <h2>确认恢复 AI</h2>
    <p>恢复后，客户下一条新消息将由 AI 回复；人工期间的旧消息不会补答。</p>
    <input id="resumeConversationId" type="hidden">
    <p id="resumeError" class="dialog-error" role="alert"></p>
    <div class="dialog-actions">
      <button id="resumeCancelButton" class="button" value="cancel">取消</button>
      <button id="resumeConfirmButton" class="button primary" type="button">恢复 AI</button>
    </div>
  </form>
</dialog>
```

每行使用 `document.createElement` 和 `textContent` 创建：会话编号、转人工原因、最近活动、等待时长、文本状态、阻塞提示、Chatwoot 新窗口链接和恢复按钮。`overdue` 必须同时显示“已超时”文字和危险样式。

- [ ] **Step 3: 实现加载、确认、恢复和可操作错误**

```javascript
async function resumeConversation(conversationId, button) {
  button.disabled = true;
  setText(byId("resumeError"), "");
  try {
    const response = await api(
      `/v1/operations/conversations/${conversationId}/resume-ai`,
      { method: "POST" }
    );
    const result = await response.json();
    byId("resumeDialog").close();
    showPageMessage(
      result.status === "already_ai" ? "该会话已经由 AI 接管。" : `会话 #${conversationId} 已恢复 AI。`
    );
    await loadHumanConversations();
  } catch (error) {
    const message = error.status === 409
      ? "该会话仍有投递中或失败任务，请先处理任务后重试。"
      : "恢复失败，AI 仍保持关闭。请检查 Chatwoot 状态后重试。";
    setText(byId("resumeError"), message);
  } finally {
    button.disabled = false;
  }
}
```

扩展 `api()` 抛出的 Error，附加 `status` 和脱敏 `detail`。`loadData()` 的 `Promise.all` 增加人工会话请求；单独恢复后只刷新人工列表，避免重复刷新所有图表。

- [ ] **Step 4: 编写 Playwright 桌面和移动交互测试**

在浏览器脚本中拦截仅新增的两个 API：GET 返回 normal、due_soon、overdue 三行；POST 首次返回 409、第二次返回 200。断言：

```python
page.click('button[data-resume-conversation="42"]')
assert page.locator("#resumeDialog").get_attribute("open") is not None
page.click("#resumeCancelButton")
assert page.locator("#resumeDialog").get_attribute("open") is None

page.click('button[data-resume-conversation="42"]')
page.click("#resumeConfirmButton")
page.wait_for_function("document.querySelector('#resumeError').textContent.includes('投递中')")
page.click("#resumeConfirmButton")
page.wait_for_function("!document.querySelector('#resumeDialog').open")
assert page.locator('[data-conversation-id="42"]').count() == 0
```

桌面断言三种文字状态，360px 断言列表、按钮和弹窗可见且 `scrollWidth <= clientWidth`。继续收集浏览器 console error。

- [ ] **Step 5: 运行静态与浏览器测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_dashboard -v`

Expected: PASS。

Run: `.\.venv\Scripts\python.exe tests\operations-dashboard-browser.py`

Expected: `PASS: Operations dashboard desktop, mobile, auth, handoff resume, filters, and CSV are verified.`

- [ ] **Step 6: 提交任务 7**

```powershell
git add app/static/operations.html tests/test_operations_dashboard.py tests/operations-dashboard-browser.py
git commit -m "feat: resume AI from the operations dashboard"
```

---

### Task 8: 真实链路验收、文档和完整回归

**Files:**
- Create: `tests/human-handoff-e2e.ps1`
- Modify: `README.md:1-190`
- Modify: `.env.example`
- Modify: `tests/compose-worker-config-test.ps1`

**Interfaces:**
- Consumes: 所有前述接口。
- Produces: 可重复执行的短 SLA 真实 Chatwoot 验收脚本和最终运行说明。
- Guarantees: 文档明确区分临时 AI 托底与恢复 AI，保留命令行恢复脚本作为应急工具。

- [ ] **Step 1: 为 Compose 环境传递和 README 关键说明编写失败检查**

在 `tests/compose-worker-config-test.ps1` 断言渲染后的 Worker 配置包含：

```powershell
if ($rendered -notmatch 'CUSTOMER_SERVICE_HUMAN_SLA_SECONDS') {
    throw 'Worker compose config does not expose the human SLA setting.'
}
if ($rendered -notmatch 'CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS') {
    throw 'Worker compose config does not expose the fallback cooldown setting.'
}
```

若 Compose 使用 `env_file` 而渲染输出不展开变量，则改为断言 `docling` 与 `worker` 都保留 `.env` 引用，并在 README 说明变量从项目 `.env` 读取。

- [ ] **Step 2: 编写短 SLA 真实 Chatwoot E2E**

`tests/human-handoff-e2e.ps1` 必须复用 `.env` 配置但不打印密钥，创建临时联系人/会话并执行以下可见断言：

```powershell
$env:CUSTOMER_SERVICE_HUMAN_SLA_SECONDS = '3'
$env:CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS = '3'

# 1. 发送“我要转人工”，轮询 ai_handoff 标签。
# 2. 发送普通 FAQ，3 秒内由人工公开回复，等待 4 秒，断言没有 human-fallback source_id。
# 3. 再发普通 FAQ，不人工回复，轮询且只允许一个 ai-cs:<id>:human-fallback，正文含“AI 临时协助”。
# 4. 发送“我的支付账户被盗”，不人工回复，轮询等待提示且正文不包含业务处理结论。
# 5. 用 X-Operations-Key POST 恢复接口，断言标签只移除 ai_handoff，其他测试标签仍存在。
# 6. 发送新 FAQ，断言收到普通 ai-cs:<id>:reply；恢复前消息没有新增补答。
```

脚本用 `try/finally` 删除临时 Chatwoot 联系人或关闭测试会话；所有 API 错误只打印 HTTP 状态和稳定步骤名，不打印响应头、token 或完整远程错误体。Worker 以短 SLA 新进程启动时，脚本退出前可靠停止该进程。

- [ ] **Step 3: 更新使用和运维文档**

README 增加：

- 人工模式下 5 分钟 SLA 的工作流程图。
- “AI 临时协助”不会改变人工所有权。
- 客服公开回复会取消尚未发送的托底；私密备注不会。
- 管理员在 `/operations` 中二次确认恢复 AI。
- 恢复后只有下一条新消息进入 RAG，旧消息不会补答。
- HTTP 409 时先处理投递中/失败任务；Chatwoot 故障时本地继续保持人工。
- `reset-ai-conversation.ps1` 仅作为看板不可用时的应急工具。
- 新配置默认值、允许范围和生产建议。

- [ ] **Step 4: 运行完整 Python 回归**

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'`

Expected: 全部 PASS，测试数大于现有 127。

Run: `.\.venv\Scripts\python.exe -m compileall -q app tests`

Expected: exit code 0，无输出。

Run: `.\.venv\Scripts\python.exe -m pip check`

Expected: `No broken requirements found.`

- [ ] **Step 5: 运行配置、RAG 和现有 E2E 回归**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\compose-worker-config-test.ps1`

Expected: PASS。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\rag-evaluation.ps1`

Expected: 8/8 PASS。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\customer-service-e2e.ps1`

Expected: PASS，正常回答、重复 Webhook、转人工、人工静默与运营数据均通过。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\chatwoot-real-webhook-e2e.ps1`

Expected: PASS，Website Widget → Chatwoot → Webhook → Worker → Dify → Chatwoot 全链路通过。

- [ ] **Step 6: 运行新人工 SLA E2E 和浏览器验收**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\human-handoff-e2e.ps1`

Expected: PASS，人工及时回复取消、普通 FAQ 临时托底、高风险等待提示、看板恢复和旧消息不补答全部通过。

Run: `.\.venv\Scripts\python.exe .\tests\operations-dashboard-browser.py`

Expected: PASS，桌面与 360px 无遮挡或水平溢出。

- [ ] **Step 7: 校验 Compose 和本地服务健康**

Run: `docker compose config -q`

Expected: exit code 0。

Run: `Invoke-RestMethod http://127.0.0.1:5001/health; Invoke-RestMethod http://127.0.0.1:5001/ready`

Expected: 分别返回 `status=ok` 与 `status=ready`。

- [ ] **Step 8: 检查最终差异和敏感信息**

```powershell
git diff --check
git status --short
git diff -- . ':!artifacts' | Select-String -Pattern 'sk-[A-Za-z0-9]|api_access_token\s*[=:]\s*[^"$]|OPERATIONS_API_KEY=[^$]' -CaseSensitive
```

Expected: 无空白错误；只有计划内文件；敏感信息扫描无命中。任何本机 `.env`、数据库、截图和 E2E 临时数据均不进入 Git。

- [ ] **Step 9: 提交任务 8**

```powershell
git add README.md .env.example tests/human-handoff-e2e.ps1 tests/compose-worker-config-test.ps1
git commit -m "test: verify human handoff SLA end to end"
```

---

## Final Acceptance Checklist

- [ ] 人工接管状态和 SLA 在 Worker 重启后仍存在。
- [ ] SLA 内人工公开回复不会出现 AI 托底；私密备注不算人工回复。
- [ ] 普通 FAQ 超时最多发送一次有知识依据的“AI 临时协助”，会话仍为 human。
- [ ] 高风险、知识不足和 Dify 故障只发送等待提示。
- [ ] 同会话在冷却期内不刷屏，不同会话仍可并行。
- [ ] AI 自己的 outgoing Webhook 不会被误判为人工回复。
- [ ] 运维列表不包含客户正文、联系人或密钥。
- [ ] 恢复按钮有二次确认；成功后只移除 `ai_handoff`。
- [ ] Chatwoot 或 SQLite 故障不会让本地会话误切换为 AI。
- [ ] 恢复前排队、迟到 Webhook 和对账旧消息不会补答。
- [ ] 全量单测、RAG 8/8、直接 E2E、真实 Widget E2E、新 SLA E2E、浏览器桌面/移动和 Compose 校验全部通过。
