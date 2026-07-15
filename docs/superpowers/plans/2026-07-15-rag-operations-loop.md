# RAG 客服运营验证闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Docling、Dify、DeepSeek、Chatwoot 和 Python 编排服务上，增加不影响客服主链路的运营事件采集、RAG 指标、知识缺口处理、CSV 导出与响应式运营页面。

**Architecture:** `CustomerService` 只依赖独立的 `OperationsRecorder` 协议，并以 best-effort 方式记录每次消息处理结果；`OperationsStore` 在现有 SQLite 文件中新建运营表，负责原子写入和只读聚合。FastAPI 提供密钥保护的运营 API 和无构建依赖的静态页面，页面把密钥保存在当前标签页的 `sessionStorage` 并通过请求头发送。

**Tech Stack:** Python 3.12、FastAPI 0.139.0、Pydantic、Python 标准库（`sqlite3`、`csv`、`hashlib`、`hmac`、`secrets`、`unicodedata`）、原生 HTML/CSS/JavaScript/SVG、unittest、PowerShell、Docker Compose。

## Global Constraints

- 不新增生产依赖，不引入 React、图表库、外部 CDN、RBAC、SSO、WebSocket 或语义聚类。
- 继续使用 `CUSTOMER_SERVICE_DB_PATH` 指向的 SQLite 文件、短连接和 WAL；只使用 `CREATE TABLE IF NOT EXISTS`，不修改现有表字段。
- 运营记录失败不得改变客服回复、转人工、消息 claim/release 或 webhook 原有行为。
- `service_events` 不保存正常问题文本、AI 回答、联系人姓名、联系方式或任何密钥。
- 只有 Dify 返回 `[HANDOFF]` 的问题进入 `knowledge_gaps`；主动转人工、投诉/法律、支付/账户安全和 Dify 故障不得进入。
- 知识缺口落库前遮盖手机号、邮箱、18 位身份证号及 12–19 位连续卡号；样例问题最多 500 字符，备注最多 1000 字符。
- 所有 `/v1/operations/*` API 使用 `X-Operations-Key`，通过常量时间比较校验 `OPERATIONS_API_KEY`；未配置返回 503，缺失或错误返回 401。
- 密钥不得进入 URL、页面源码、日志、对象 `repr`、测试输出或 HTTP 错误正文；浏览器端只写入 `sessionStorage`。
- CSV 使用 UTF-8 BOM，并对以 `= + - @`、制表符或回车开头的单元格加单引号。
- 现有 Chatwoot、Dify、知识入库和网页客服接口保持兼容；现有回归测试必须继续通过。
- 工作区不是 Git 仓库，本计划不执行 commit、分支或 push；每个任务以测试通过作为检查点。

## File Structure

- Create `app/operations_models.py`: 运营结果/原因类型、数据对象、`OperationsRecorder` 协议和空实现。
- Create `app/operations_privacy.py`: PII 脱敏、规范化和 SHA-256 指纹。
- Create `app/operations_store.py`: 运营表初始化、事件/缺口写入、指标聚合、列表和导出数据查询。
- Create `app/operations_csv.py`: 安全 CSV 单元格处理和 UTF-8 BOM 编码。
- Create `app/operations_api.py`: 独立运营配置、密钥鉴权和 `/v1/operations/*` 路由。
- Create `app/static/operations.html`: 原生响应式运营页面。
- Modify `app/customer_service.py`: 在所有处理分支 best-effort 记录结果、原因和耗时。
- Modify `app/main.py`: 注入真实 `OperationsStore`、挂载运营路由并提供 `/operations` 页面。
- Create `tests/test_operations_privacy.py`, `tests/test_operations_store.py`, `tests/test_operations_csv.py`, `tests/test_operations_api.py`, `tests/test_operations_dashboard.py`.
- Modify `tests/test_customer_service.py`: 覆盖所有结果、原因、重试覆盖与记录失败隔离。
- Create `scripts/configure-operations.ps1` and `tests/configure-operations-env-test.ps1`: 安全生成、复用和轮换运营密钥。
- Modify `.env.example`, `compose.yaml`, `README.md`, `tests/customer-service-e2e.ps1`: 配置、运行说明和真实链路验收。

---

### Task 1: 定义运营记录接口与隐私处理

**Files:**
- Create: `app/operations_models.py`
- Create: `app/operations_privacy.py`
- Create: `tests/test_operations_privacy.py`

**Interfaces:**
- Produces: `Outcome`, `Reason`, `GapStatus`, `ServiceEvent`, `KnowledgeGap`, `OperationsRecorder`, `NullOperationsRecorder`.
- Produces: `redact_question(text: str) -> str`, `normalize_question(text: str) -> str`, `question_fingerprint(text: str) -> str`.
- Consumes: 原始客户问题；不依赖数据库或客服模块。

- [ ] **Step 1: 写隐私与指纹失败测试**

```python
class OperationsPrivacyTests(unittest.TestCase):
    def test_redacts_supported_personal_identifiers_and_truncates(self):
        text = (
            "联系 13812345678、buyer@example.com，身份证11010519491231002X，"
            "卡号 6222021234567890123。" + "问" * 600
        )
        redacted = redact_question(text)
        self.assertNotIn("13812345678", redacted)
        self.assertNotIn("buyer@example.com", redacted)
        self.assertNotIn("11010519491231002X", redacted)
        self.assertNotIn("6222021234567890123", redacted)
        self.assertIn("[手机号]", redacted)
        self.assertIn("[邮箱]", redacted)
        self.assertIn("[身份证]", redacted)
        self.assertIn("[卡号]", redacted)
        self.assertLessEqual(len(redacted), 500)

    def test_fingerprint_merges_nfkc_case_whitespace_and_edge_punctuation(self):
        variants = ["  ＡＢＣ  保修？ ", "abc 保修", "…ABC\t保修!!!"]
        fingerprints = {question_fingerprint(item) for item in variants}
        self.assertEqual(len(fingerprints), 1)

    def test_fingerprint_is_based_on_redacted_text(self):
        self.assertEqual(
            question_fingerprint("手机号 13812345678 怎么改？"),
            question_fingerprint("手机号 13900001111 怎么改？"),
        )
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_privacy -v`

Expected: FAIL，提示 `app.operations_privacy` 不存在。

- [ ] **Step 3: 实现模型、协议与空记录器**

```python
Outcome = Literal["replied", "handed_off", "ignored_human_mode", "duplicate", "failed"]
Reason = Literal[
    "explicit_human_request", "complaint_or_legal",
    "payment_or_account_security", "knowledge_gap", "dify_error",
    "chatwoot_error", "internal_error",
]
GapStatus = Literal["pending", "resolved", "ignored"]

class OperationsRecorder(Protocol):
    def record_event(
        self, *, message_id: int, conversation_id: int, outcome: Outcome,
        reason: Reason | None, duration_ms: int, created_at: int | None = None,
    ) -> None: ...
    def record_knowledge_gap(
        self, *, question: str, conversation_id: int,
        created_at: int | None = None,
    ) -> None: ...

class NullOperationsRecorder:
    def record_event(self, **kwargs: object) -> None:
        return None
    def record_knowledge_gap(self, **kwargs: object) -> None:
        return None
```

`ServiceEvent` 和 `KnowledgeGap` 使用 `@dataclass(frozen=True)`，字段名与设计规格中的两张表完全一致；`KnowledgeGap.status` 使用 `GapStatus`。

- [ ] **Step 4: 实现脱敏、规范化与指纹**

```python
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IDENTITY = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_CARD = re.compile(r"(?<!\d)\d{12,19}(?!\d)")

def redact_question(text: str) -> str:
    value = _PHONE.sub("[手机号]", text)
    value = _EMAIL.sub("[邮箱]", value)
    value = _IDENTITY.sub("[身份证]", value)
    value = _CARD.sub("[卡号]", value)
    return value.strip()[:500]

def normalize_question(text: str) -> str:
    value = unicodedata.normalize("NFKC", redact_question(text)).lower()
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(string.punctuation + "，。！？；：‘’“”（）【】《》… ")

def question_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode("utf-8")).hexdigest()
```

- [ ] **Step 5: 运行聚焦测试确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_privacy -v`

Expected: 3 tests PASS。

---

### Task 2: 建立运营事件与知识缺口存储

**Files:**
- Create: `app/operations_store.py`
- Create: `tests/test_operations_store.py`

**Interfaces:**
- Consumes: Task 1 的 `Outcome`, `Reason`, `GapStatus`, `KnowledgeGap`, `redact_question`, `question_fingerprint`.
- Produces: `OperationsStore(database_path)`；实现 `record_event`, `record_knowledge_gap`, `list_gaps`, `update_gap`。
- `list_gaps(*, status: GapStatus | Literal["all"] = "pending", limit: int = 50, offset: int = 0) -> tuple[list[KnowledgeGap], int]`.
- `update_gap(gap_id: int, *, status: GapStatus, note: str, updated_at: int | None = None) -> KnowledgeGap | None`.

- [ ] **Step 1: 写表结构、事件校验和缺口 upsert 失败测试**

```python
def test_records_event_without_question_text(self):
    self.store.record_event(
        message_id=101, conversation_id=9, outcome="replied",
        reason=None, duration_ms=125, created_at=1_720_000_000,
    )
    events = self.store.export_events(window="all", now=1_720_000_100)
    self.assertEqual(events[0]["outcome"], "replied")
    self.assertNotIn("question", events[0])

def test_gap_upsert_merges_normalized_variants_and_preserves_manual_status(self):
    self.store.record_knowledge_gap(
        question=" ＡＢＣ  保修？ ", conversation_id=9, created_at=100,
    )
    gap = self.store.list_gaps(status="all")[0][0]
    self.store.update_gap(gap.id, status="resolved", note="已补充", updated_at=150)
    self.store.record_knowledge_gap(
        question="abc 保修", conversation_id=10, created_at=200,
    )
    merged = self.store.list_gaps(status="all")[0][0]
    self.assertEqual(merged.occurrence_count, 2)
    self.assertEqual(merged.status, "resolved")
    self.assertEqual(merged.note, "已补充")
    self.assertEqual(merged.last_conversation_id, 10)
```

同时断言非法 outcome/reason 组合、负耗时、非正整数 ID、`limit` 不在 1–200、负 `offset`、备注超过 1000 字符时抛出 `ValueError`。

- [ ] **Step 2: 运行测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_store -v`

Expected: FAIL，提示 `app.operations_store` 不存在。

- [ ] **Step 3: 实现只增量建表的初始化**

```sql
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS service_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN (
        'replied','handed_off','ignored_human_mode','duplicate','failed'
    )),
    reason TEXT,
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_service_events_message
    ON service_events(message_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_service_events_created
    ON service_events(created_at);
CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    sample_question TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count > 0),
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    last_conversation_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','resolved','ignored')),
    note TEXT NOT NULL DEFAULT '' CHECK(length(note) <= 1000),
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_sort
    ON knowledge_gaps(occurrence_count DESC, last_seen_at DESC);
```

连接方式复制 `SQLiteConversationStore` 的 `_connect` 模式：`timeout=5`、`row_factory=sqlite3.Row`、正常 commit、异常 rollback、始终 close。

- [ ] **Step 4: 实现严格校验和原子 upsert**

`record_event` 对 outcome/reason 使用以下精确规则：`handed_off` 只允许五个 handoff 原因，`failed` 只允许两个失败原因，其余 outcome 必须为 `reason=None`。`created_at=None` 时取 `int(time.time())`。

```sql
INSERT INTO knowledge_gaps (
    fingerprint, sample_question, occurrence_count, first_seen_at,
    last_seen_at, last_conversation_id, status, note, updated_at
) VALUES (?, ?, 1, ?, ?, ?, 'pending', '', ?)
ON CONFLICT(fingerprint) DO UPDATE SET
    occurrence_count = knowledge_gaps.occurrence_count + 1,
    last_seen_at = excluded.last_seen_at,
    last_conversation_id = excluded.last_conversation_id
```

`list_gaps` 的排序固定为 `occurrence_count DESC, last_seen_at DESC, id ASC`，返回当前筛选的分页数据和总数。`update_gap` 保留样例、次数和首末时间，只修改状态、备注和 `updated_at`；找不到返回 `None`。

- [ ] **Step 5: 运行存储测试确认 GREEN，并确认旧表仍可读写**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_store tests.test_conversation_store -v`

Expected: 新旧存储测试全部 PASS。

---

### Task 3: 实现最终结果指标、趋势和安全 CSV

**Files:**
- Modify: `app/operations_store.py`
- Create: `app/operations_csv.py`
- Modify: `tests/test_operations_store.py`
- Create: `tests/test_operations_csv.py`

**Interfaces:**
- Produces: `OperationsStore.get_summary(window: Literal["7d","30d","all"], *, now: int | None = None) -> dict[str, object]`.
- Produces: `OperationsStore.export_events(...)`, `OperationsStore.export_gaps(...)`.
- Produces: `encode_csv(headers: tuple[str, ...], rows: Iterable[Mapping[str, object]]) -> bytes`.

- [ ] **Step 1: 写重试覆盖、零分母、窗口和趋势失败测试**

构造以下固定事件：消息 1 先 `failed/chatwoot_error` 后 `replied`；消息 2 `handed_off/knowledge_gap`；消息 3 `ignored_human_mode`；消息 4 `handed_off/dify_error`；另写入一条 `duplicate`。断言：

```python
summary = self.store.get_summary("7d", now=700_000)
self.assertEqual(summary["unique_customer_messages"], 4)
self.assertEqual(summary["ai_eligible_messages"], 3)
self.assertEqual(summary["replied"], 1)
self.assertEqual(summary["handed_off"], 2)
self.assertEqual(summary["failed"], 0)
self.assertEqual(summary["system_errors"], 1)
self.assertEqual(summary["duplicate_webhooks"], 1)
self.assertEqual(summary["answer_rate"], {"numerator": 1, "denominator": 3, "value": 1 / 3})
self.assertEqual(summary["knowledge_gap_rate"], {"numerator": 1, "denominator": 3, "value": 1 / 3})
```

再用空库断言所有百分比 `value` 为 `0`，不用 `NaN`；用 8 天前事件断言不进入 `7d`、进入 `30d` 和 `all`。趋势数组必须按 UTC 日期升序，字段固定为 `date`, `customer_messages`, `replied`, `handed_off`, `knowledge_gaps`。

- [ ] **Step 2: 运行聚焦测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_store -v`

Expected: FAIL，提示 `get_summary` 不存在。

- [ ] **Step 3: 实现“每消息最后一个非重复事件”查询**

```sql
WITH ranked AS (
    SELECT e.*,
           ROW_NUMBER() OVER (PARTITION BY message_id ORDER BY id DESC) AS rank
    FROM service_events AS e
    WHERE outcome <> 'duplicate'
), final_events AS (
    SELECT * FROM ranked
    WHERE rank = 1 AND created_at >= :cutoff
)
SELECT * FROM final_events
```

`all` 不添加时间下限；`7d` 和 `30d` 的 cutoff 分别为 `now - 7*86400`、`now - 30*86400`。重复 webhook 单独按原始 `duplicate` 事件的 `created_at` 统计。平均耗时仅包含最终 `replied` 和 `handed_off`，无数据时为 `0`。转人工原因分布始终返回五个原因键，缺失值为 `0`。

- [ ] **Step 4: 写 CSV 编码失败测试**

```python
payload = encode_csv(
    ("问题", "备注"),
    [{"问题": "退款多久", "备注": "=HYPERLINK(\"bad\")"},
     {"问题": "保修", "备注": "\tcmd"}],
)
self.assertTrue(payload.startswith(codecs.BOM_UTF8))
decoded = payload.decode("utf-8-sig")
self.assertIn("退款多久", decoded)
self.assertIn("'=HYPERLINK", decoded)
self.assertIn("'\tcmd", decoded)
```

- [ ] **Step 5: 实现导出查询和 CSV 编码**

```python
def _safe_cell(value: object) -> str:
    rendered = "" if value is None else str(value)
    if rendered.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + rendered
    return rendered

def encode_csv(headers, rows) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: _safe_cell(row.get(header)) for header in headers})
    return codecs.BOM_UTF8 + output.getvalue().encode("utf-8")
```

事件导出列固定为 `id,message_id,conversation_id,outcome,reason,duration_ms,created_at`；缺口导出列固定为 `id,sample_question,occurrence_count,first_seen_at,last_seen_at,last_conversation_id,status,note,updated_at`。两种导出都按窗口过滤，事件按 `created_at DESC,id DESC`，缺口按 `last_seen_at DESC,id DESC`。

- [ ] **Step 6: 运行指标与 CSV 测试确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_store tests.test_operations_csv -v`

Expected: 所有指标、窗口、趋势和 CSV 测试 PASS。

---

### Task 4: 把运营记录接入客服所有处理分支

**Files:**
- Modify: `app/customer_service.py`
- Modify: `tests/test_customer_service.py`

**Interfaces:**
- Consumes: Task 1 的 `OperationsRecorder`, `NullOperationsRecorder`, `Outcome`, `Reason`.
- `CustomerService.__init__` 新增可选 `operations_recorder: OperationsRecorder | None = None` 和可测试时钟 `monotonic: Callable[[], float] = time.perf_counter`。
- `HandlingResult` 的公共结构保持只有 `status`，不改变 webhook 响应。

- [ ] **Step 1: 增加内存记录器并写各分支失败测试**

```python
class FakeOperationsRecorder:
    def __init__(self):
        self.events = []
        self.gaps = []
        self.fail = False
    def record_event(self, **event):
        if self.fail:
            raise RuntimeError("operations unavailable")
        self.events.append(event)
    def record_knowledge_gap(self, **gap):
        if self.fail:
            raise RuntimeError("operations unavailable")
        self.gaps.append(gap)
```

分别断言正常回答=`replied/None`，重复=`duplicate/None`，人工模式=`ignored_human_mode/None`，三类规则命中各自 reason，Dify 故障且转接成功=`handed_off/dify_error`，Dify sentinel=`handed_off/knowledge_gap` 且只此分支写 `gaps`，Chatwoot 发送异常=`failed/chatwoot_error` 并继续向上抛出。用先失败后重试成功断言同一 `message_id` 留下两个事件。

- [ ] **Step 2: 运行聚焦测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_customer_service -v`

Expected: FAIL，因为构造器未接受 recorder 且没有事件。

- [ ] **Step 3: 实现 best-effort 记录辅助方法**

```python
def _record_event(self, message, *, outcome, reason, started_at):
    duration_ms = max(0, int((self._monotonic() - started_at) * 1000))
    try:
        self._operations.record_event(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            outcome=outcome,
            reason=reason,
            duration_ms=duration_ms,
        )
    except Exception:
        logger.warning("Operations event recording failed.", exc_info=True)

def _record_gap(self, message):
    try:
        self._operations.record_knowledge_gap(
            question=message.content,
            conversation_id=message.conversation_id,
        )
    except Exception:
        logger.warning("Operations knowledge-gap recording failed.", exc_info=True)
```

日志正文不得插入问题文本、异常参数、HTTP header 或任何密钥。

- [ ] **Step 4: 在精确处理点记录结果**

`handle` 首行保存 `started_at`。claim 失败立即记 `duplicate`；人工模式 complete 后记 `ignored_human_mode`；规则命中时保留 `reason = handoff_reason(message.content)` 并在转接、complete 后记 `handed_off/reason`；Dify 异常转接完成后记 `handed_off/dify_error`；sentinel 转接、complete 后先记 `handed_off/knowledge_gap` 再 `_record_gap`；正常发送、保存、complete 后记 `replied`。

外层异常分支先按现有行为 `release_message`，然后以 `isinstance(exc, ChatwootAPIError)` 选择 `chatwoot_error` 或 `internal_error`，best-effort 记录 `failed`，最后原样 `raise`。

- [ ] **Step 5: 验证运营存储故障不影响主链路**

将 `FakeOperationsRecorder.fail=True`，断言正常问题仍发送 Chatwoot、保存 Dify 会话、complete message 并返回 `HandlingResult(status="replied")`；sentinel 仍完成转人工。使用 `assertLogs("app.customer_service", level="WARNING")` 只断言固定日志文本，不把问题或密钥加入日志。

- [ ] **Step 6: 运行客服与策略回归确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_customer_service tests.test_handoff_policy -v`

Expected: 新增运营断言和原有客服行为全部 PASS。

---

### Task 5: 提供密钥保护的运营 API

**Files:**
- Create: `app/operations_api.py`
- Modify: `app/main.py`
- Modify: `app/settings.py`
- Create: `tests/test_operations_api.py`
- Modify: `tests/test_settings.py`

**Interfaces:**
- Produces: `OperationsSettings.from_env(environ=None)`，包含 `api_key`（`repr=False`）和 `database_path`。
- Produces: `operations_router`，包含 summary、gaps、PATCH gap、export.csv。
- Consumes: Task 2/3 的 `OperationsStore` 和 `encode_csv`。

- [ ] **Step 1: 写独立配置和鉴权失败测试**

使用 `unittest.mock.patch.dict(os.environ, ..., clear=True)` 和 `fastapi.testclient.TestClient(app)`：

```python
def test_operations_api_returns_503_when_key_is_not_configured(self):
    response = self.client.get("/v1/operations/summary?window=7d")
    self.assertEqual(response.status_code, 503)

def test_operations_api_returns_401_for_missing_or_wrong_key(self):
    os.environ["OPERATIONS_API_KEY"] = "correct-secret"
    self.assertEqual(self.client.get("/v1/operations/summary?window=7d").status_code, 401)
    response = self.client.get(
        "/v1/operations/summary?window=7d",
        headers={"X-Operations-Key": "wrong-secret"},
    )
    self.assertEqual(response.status_code, 401)
    self.assertNotIn("correct-secret", response.text)
```

再断言正确密钥返回 200，非法 window/status/limit/note 返回 422，不存在 gap ID 返回 404，正确 PATCH 返回更新后的对象，CSV 返回 `text/csv` 且以 BOM 开头。

- [ ] **Step 2: 运行 API 与设置测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_api tests.test_settings -v`

Expected: FAIL，因为运营配置与路由尚不存在。

- [ ] **Step 3: 实现与客服配置解耦的 `OperationsSettings`**

```python
@dataclass(frozen=True)
class OperationsSettings:
    api_key: str = field(repr=False)
    database_path: Path

    @classmethod
    def from_env(cls, environ=None):
        source = os.environ if environ is None else environ
        api_key = source.get("OPERATIONS_API_KEY", "").strip()
        raw_path = source.get(
            "CUSTOMER_SERVICE_DB_PATH", ".runtime/customer-service/state.db"
        ).strip()
        if not raw_path:
            raise ValueError("Operations configuration is invalid.")
        return cls(api_key=api_key, database_path=Path(raw_path))
```

未配置 key 不抛配置解析异常，由鉴权依赖统一返回 503；`repr(settings)` 不包含 key。

- [ ] **Step 4: 实现常量时间鉴权和严格请求模型**

```python
def require_operations_settings(request: Request) -> OperationsSettings:
    settings = OperationsSettings.from_env()
    if not settings.api_key:
        raise HTTPException(503, "Operations API is not configured.")
    provided = request.headers.get("X-Operations-Key", "")
    if not provided or not hmac.compare_digest(provided, settings.api_key):
        raise HTTPException(401, "Invalid operations key.")
    return settings

class GapUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "resolved", "ignored"]
    note: str = Field(max_length=1000)
```

window 使用 `Literal["7d","30d","all"]`；status 使用 `Literal["pending","resolved","ignored","all"]`；limit 使用 `Query(50, ge=1, le=200)`；offset 使用 `Query(0, ge=0)`；gap ID 使用 `Path(gt=0)`。

- [ ] **Step 5: 实现四个 API 并在主应用挂载**

每个请求用 `OperationsStore(settings.database_path)`；summary 返回 store 字典；gaps 返回 `{"items": [asdict(item)], "total": total, "limit": limit, "offset": offset}`；PATCH 找不到时抛 404；CSV 根据 kind 选择固定 headers 和查询数据，并返回：

```python
return Response(
    content=encode_csv(headers, rows),
    media_type="text/csv; charset=utf-8",
    headers={"Content-Disposition": f'attachment; filename="operations-{kind}-{window}.csv"'},
)
```

在 `app/main.py` 执行 `app.include_router(operations_router)`，并把 `get_customer_service` 的 recorder 改为 `OperationsStore(settings.customer_service_db_path)`。

- [ ] **Step 6: 运行接口与现有 webhook 回归确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_api tests.test_settings tests.test_chatwoot_webhook -v`

Expected: 所有运营 HTTP 状态、密钥保密和现有 webhook 测试 PASS。

---

### Task 6: 构建原生响应式运营页面

**Files:**
- Create: `app/static/operations.html`
- Modify: `app/main.py`
- Create: `tests/test_operations_dashboard.py`

**Interfaces:**
- Produces: `GET /operations` 静态页面。
- Consumes: Task 5 的 summary、gaps、PATCH 和 CSV API；密钥只从 `sessionStorage.getItem("operationsKey")` 读取。

- [ ] **Step 1: 写页面安全和结构失败测试**

```python
def test_operations_page_is_served(self):
    response = TestClient(app).get("/operations")
    self.assertEqual(response.status_code, 200)
    self.assertIn("RAG 客服运营看板", response.text)

def test_dashboard_has_no_external_assets_and_keeps_key_in_session_storage(self):
    source = Path("app/static/operations.html").read_text(encoding="utf-8")
    self.assertNotIn("https://cdn", source)
    self.assertNotIn("localStorage", source)
    self.assertIn("sessionStorage", source)
    self.assertIn('"X-Operations-Key"', source)
    self.assertNotIn("?key=", source)
    self.assertNotIn(".innerHTML", source)
```

再断言存在 `7d/30d/all`、KPI 容器、`svg`、status filter、limit/offset 分页控件、note 编辑、保存、刷新和两个 CSV 导出按钮。

- [ ] **Step 2: 运行页面测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_dashboard -v`

Expected: FAIL，因为页面文件和路由不存在。

- [ ] **Step 3: 实现语义化 HTML 与响应式 CSS**

页面包含：密钥解锁对话框、页头服务状态和 Chatwoot/Dify 链接、窗口切换、六张 KPI 卡、趋势 SVG、原因条形图、缺口状态筛选、分页、缺口卡片、状态下拉、备注 textarea、保存反馈、手动刷新、events/gaps CSV 按钮。

桌面端使用 CSS Grid；`@media (max-width: 720px)` 下 KPI 变单列、控件可换行、知识缺口使用卡片布局；`body { overflow-x: hidden; }`，表单控件 `max-width: 100%`，保证 360px 无横向遮挡。

- [ ] **Step 4: 实现安全 DOM 渲染和 API 调用**

```javascript
const KEY_NAME = "operationsKey";
function apiHeaders() {
  return { "X-Operations-Key": sessionStorage.getItem(KEY_NAME) || "" };
}
function setText(node, value) {
  node.textContent = value == null ? "—" : String(value);
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...apiHeaders(), ...(options.headers || {}) },
  });
  if (response.status === 401 || response.status === 503) {
    sessionStorage.removeItem(KEY_NAME);
    document.getElementById("keyDialog").showModal();
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response;
}
```

所有接口字符串使用 `textContent` 或 `setAttribute`；图表使用 `document.createElementNS("http://www.w3.org/2000/svg", ...)`；不使用 `innerHTML`。CSV 下载通过带 header 的 `fetch` 获取 Blob，再创建临时 object URL，不把 key 放入链接。

- [ ] **Step 5: 在主应用提供静态页面**

```python
OPERATIONS_PAGE = Path(__file__).parent / "static" / "operations.html"

@app.get("/operations", include_in_schema=False)
def operations_page() -> FileResponse:
    return FileResponse(OPERATIONS_PAGE, media_type="text/html; charset=utf-8")
```

- [ ] **Step 6: 运行页面和 API 测试确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_operations_dashboard tests.test_operations_api -v`

Expected: 页面结构、安全断言和 API 测试全部 PASS。

---

### Task 7: 配置脚本、Compose 与使用文档

**Files:**
- Create: `scripts/configure-operations.ps1`
- Create: `tests/configure-operations-env-test.ps1`
- Modify: `.env.example`
- Modify: `compose.yaml`
- Modify: `README.md`

**Interfaces:**
- Produces: `configure-operations.ps1 [-EnvPath <path>] [-Rotate]`。
- Compose 向 `docling` 容器传入 `OPERATIONS_API_KEY`。

- [ ] **Step 1: 写配置脚本失败测试**

测试在临时目录创建 `.env`，首次运行后断言 key 非空且长度至少 43；第二次运行不带 `-Rotate` 断言值不变；第三次带 `-Rotate` 断言值改变；每次输出都不得包含新旧 key；`UNRELATED_SETTING=keep-me` 必须保留。测试 finally 只删除经 `GetFullPath` 验证位于系统临时目录下的测试目录。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\configure-operations-env-test.ps1`

Expected: FAIL，因为脚本不存在。

- [ ] **Step 2: 实现安全生成、复用和轮换**

```powershell
param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [switch]$Rotate
)
$ErrorActionPreference = "Stop"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.AddRange([IO.File]::ReadAllLines($EnvPath, [Text.Encoding]::UTF8))
$existing = $lines | Where-Object { $_.StartsWith("OPERATIONS_API_KEY=") } |
    Select-Object -First 1
if ($Rotate.IsPresent -or [string]::IsNullOrWhiteSpace(($existing -replace '^OPERATIONS_API_KEY=', ''))) {
    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $key = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
} else {
    $key = $existing.Substring("OPERATIONS_API_KEY=".Length)
}
```

用与现有配置脚本相同的 `Set-EnvValue` 逻辑原位更新，UTF-8 无 BOM 写回；只输出 `PASS: Operations API key is configured.`，finally 将 `$key` 和 `$existing` 置 `$null`。

- [ ] **Step 3: 接入环境与 Compose**

`.env.example` 新增 `OPERATIONS_API_KEY=` 和中文说明；`compose.yaml` 的 docling environment 新增：

```yaml
OPERATIONS_API_KEY: "${OPERATIONS_API_KEY:-}"
```

不改端口、卷或现有环境变量。

- [ ] **Step 4: 更新 README**

在启动步骤中加入：

```powershell
.\scripts\configure-operations.ps1
.\scripts\run-docling.ps1
```

说明访问 `http://127.0.0.1:5001/operations`、密钥从本地 `.env` 手动复制且只存当前标签页、7/30/all 指标含义、知识缺口三种状态、CSV 导出、防止运营故障影响客服主链路，以及 Chatwoot/Dify 继续负责哪些原生报表。生产章节保留服务器、域名、HTTPS、备份和密钥轮换待办。

- [ ] **Step 5: 运行配置测试并检查 Compose 展开**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\configure-operations-env-test.ps1`

Expected: `PASS: Operations API key generation, reuse, rotation, and secret-safe output are verified.`

Run: `docker compose config --quiet`

Expected: exit code 0，且不输出真实密钥。

---

### Task 8: 扩展真实链路验收并完成全量回归

**Files:**
- Modify: `tests/customer-service-e2e.ps1`
- Modify: `README.md`

**Interfaces:**
- Consumes: 已运行的本地 Dify、Chatwoot、AI 服务以及 `.env` 中的运营 key。
- Produces: 已知问答、知识缺口、规则转人工、重复 webhook、人工模式静默和运营 API 的单次真实验收结果。

- [ ] **Step 1: 在 E2E 脚本增加运营断言**

保留现有临时联系人/会话创建和 finally 清理。在已有已知问答、duplicate、主动转人工和人工模式静默后，使用 `X-Operations-Key` 查询 summary，断言新增消息体现在 `replied`、`explicit_human_request`、`duplicate_webhooks` 和 `ignored_human_mode` 中；用独立新会话发送规范化后相同的两个未知保修问题，断言只新增/累加同一 `pending` gap，随后 PATCH 为 `resolved` 并验证备注。测试不得打印 key 或 API token。

- [ ] **Step 2: 运行全部 Python 单元测试**

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v`

Expected: 原有 47 个测试和所有新增测试全部 PASS。

- [ ] **Step 3: 运行静态与依赖检查**

Run: `.\.venv\Scripts\python.exe -m compileall -q app tests`

Expected: exit code 0。

Run: `.\.venv\Scripts\python.exe -m pip check`

Expected: `No broken requirements found.`

- [ ] **Step 4: 运行配置、RAG 和真实客服回归**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\configure-operations-env-test.ps1`

Expected: PASS，且无密钥输出。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\rag-evaluation.ps1`

Expected: 8/8 PASS。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\customer-service-e2e.ps1`

Expected: 客服回复、幂等、所有转人工分支、人工静默和运营闭环 PASS。

- [ ] **Step 5: 构建并验证容器健康**

Run: `docker compose build docling`

Expected: image build exit code 0，CPU-only Torch 检查与 `pip check` PASS。

Run: `docker compose up -d docling; docker compose ps`

Expected: `docling` 进入 healthy；`GET http://127.0.0.1:5001/health` 返回 `{"status":"ok"}`。

- [ ] **Step 6: 浏览器验收桌面与 360px 页面**

打开 `/operations`，分别在常见桌面宽度和 360×800 视口验证：错误 key 显示安全提示；正确 key 能加载 7d/30d/all；KPI、SVG 趋势和原因分布可见；状态筛选、分页、备注保存、手动刷新和两个 CSV 下载可用；移动端无横向遮挡且缺口为卡片布局；浏览器控制台无错误。截图只包含演示数据，不包含 key 输入值或开发者工具请求头。

- [ ] **Step 7: 在 README 记录最终验证命令和边界**

把新增单元测试、配置脚本、运营 E2E、浏览器验收和 Docker 健康命令加入“测试与验收”；明确当前仍是本地单机 SQLite，服务器、域名、HTTPS、备份、告警和多副本迁移属于下一阶段。

---

## Self-Review

- Spec coverage: 事件五种 outcome、七种 reason、最后非重复结果、重试覆盖、窗口、零分母、趋势、缺口 upsert/状态保留、四类脱敏、API 鉴权、422/404、CSV BOM/注入防护、响应式页面、密钥脚本、Compose、真实 E2E 和回归均有对应任务。
- Placeholder scan: 计划中没有待填写项、未定义接口或“照前面做”的占位步骤；每个新增接口都给出了精确签名、字段、测试断言和命令。
- Type consistency: `Outcome`、`Reason`、`GapStatus`、`OperationsRecorder`、`OperationsStore`、window/status literal 和 API 请求模型在生产代码、测试及页面中使用同一命名。
- Compatibility: `HandlingResult` 和现有 webhook 响应不变，运营配置独立解析，缺失 `OPERATIONS_API_KEY` 只禁用运营 API。
