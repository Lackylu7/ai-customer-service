# 可重复交付的单企业电商客服实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将星河优选演示项目改造成可通过企业配置和一条初始化命令重复交付给不同电商企业的单企业 AI 客服版本。

**Architecture:** 使用受版本控制的 JSON 示例配置与被忽略的企业本地配置分离公开品牌信息和私密部署信息；Python 配置模块作为 PowerShell、FastAPI、Dify 和 Chatwoot 初始化的唯一校验来源。每个部署实例仍只连接一个 Dify 知识库与 Chatflow、一组 Chatwoot 资源和一个状态数据库，不引入多租户路由。

**Tech Stack:** Python 3.12 标准库、FastAPI 0.139、unittest、PowerShell、Ruby on Rails runner、Dify 1.15、Chatwoot、Docker Compose。

## Global Constraints

- 默认使用中文文档和用户文案；第三方字段、环境变量和协议名称保留英文。
- 不新增生产依赖；配置读取使用 Python 标准库 `json`，Dify 容器内继续使用其已有的 `yaml` 与 SQLAlchemy。
- 一套运行实例只服务一家企业；不得增加通过请求参数切换企业的逻辑。
- `config/enterprise.example.json` 保留星河优选演示配置；真实商家使用被 Git 忽略的 `config/enterprise.local.json`。
- DeepSeek、Dify、Chatwoot 和运营密钥只允许进入被忽略的 `.env` 或权限为 0600 的临时文件，不得进入配置、代码、日志、测试输出或提交。
- 现有 Docling 文档导入、Chatwoot webhook、RAG、人工转接、运营看板和 CSV 行为必须保持兼容。
- 所有 PowerShell 文件写入继续使用 UTF-8；脚本必须幂等，不删除既有 Dify、Chatwoot 或 SQLite 数据。
- 每项行为改动先建立失败验证，再实现最小修改并执行针对性回归。

---

### Task 1: 修复知识缺口 E2E 随机误报

**Files:**
- Modify: `tests/customer-service-e2e.ps1:240-255`

**Interfaces:**
- Consumes: 运营接口返回的 `KnowledgeGap.last_conversation_id` 与本次创建的 `$gapConversationIdOne`。
- Produces: 不依赖问题原文或随机 GUID 的确定性知识缺口匹配。

- [ ] **Step 1: 用确定输入证明旧匹配方式会被合法脱敏破坏**

运行：

```powershell
$testId = "abc123456789012def"
$stored = "E2E-abc[卡号]def: what is the teleportation octopus policy code?"
$count = @(@([pscustomobject]@{ sample_question = $stored }) |
    Where-Object { $_.sample_question -like "*$testId*" }).Count
if ($count -ne 0) { throw "Old lookup unexpectedly matched." }
Write-Output "RED: Full test IDs cannot safely identify redacted questions."
```

预期：输出 `RED: Full test IDs cannot safely identify redacted questions.`，证明原断言的标识选择错误。

- [ ] **Step 2: 改用会话 ID 匹配缺口**

把原问题文本匹配替换为：

```powershell
$matchingGaps = @(
    $gapList.items |
        Where-Object {
            [int]$_.last_conversation_id -eq $gapConversationIdOne
        }
)
if ($matchingGaps.Count -ne 1) {
    throw "Unknown question was not added to the knowledge-gap queue."
}
if ([int]$matchingGaps[0].occurrence_count -ne 1) {
    throw "Knowledge-gap occurrence count is incorrect."
}
```

- [ ] **Step 3: 连续执行三次真实 E2E**

```powershell
1..3 | ForEach-Object {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\customer-service-e2e.ps1
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

预期：三次均输出 `PASS: Customer-service reply...`。

- [ ] **Step 4: 提交测试稳定性修复**

```powershell
git add tests/customer-service-e2e.ps1
git commit -m "test: make knowledge gap e2e lookup deterministic"
```

---

### Task 2: 增加企业配置模型和严格校验

**Files:**
- Create: `config/enterprise.example.json`
- Create: `app/enterprise_config.py`
- Create: `tests/test_enterprise_config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `EnterpriseConfig`、`load_enterprise_config(path)`、`EnterpriseConfig.to_mapping()` 和 `python -m app.enterprise_config --path ... --print-json`。
- Consumes: UTF-8 JSON 文件，不读取 `.env` 或任何密钥。

- [ ] **Step 1: 添加可运行的星河优选示例配置**

创建 `config/enterprise.example.json`：

```json
{
  "schema_version": 1,
  "company": {
    "name": "星河优选",
    "industry": "ecommerce",
    "website_url": "http://127.0.0.1:8000"
  },
  "assistant": {
    "name": "星河优选 AI 客服",
    "welcome_title": "你好，欢迎咨询星河优选",
    "welcome_message": "请告诉我你想了解的商品、物流或售后问题。",
    "handoff_message": "正在为你转接人工客服，请稍候。"
  },
  "dify": {
    "dataset_name": "星河优选客服知识库",
    "app_name": "星河优选 AI 客服",
    "indexing_technique": "economy"
  },
  "chatwoot": {
    "inbox_name": "星河优选网站客服",
    "widget_color": "#1f6feb"
  },
  "locale": "zh_CN",
  "business_timezone": "Asia/Shanghai"
}
```

在 `.env.example` 增加：

```text
# 企业公开品牌配置；真实商家复制示例为被忽略的 enterprise.local.json。
ENTERPRISE_CONFIG_PATH=config/enterprise.example.json
```

- [ ] **Step 2: 编写失败的配置加载测试**

创建 `tests/test_enterprise_config.py`，使用 `TemporaryDirectory` 写入配置，覆盖：

```python
def test_valid_config_loads_as_immutable_values(self) -> None:
    config = load_enterprise_config(self.write_config(self.valid_payload()))
    self.assertEqual(config.company.name, "测试商城")
    self.assertEqual(config.assistant.handoff_message, "正在转接人工客服。")
    self.assertEqual(config.dify.indexing_technique, "economy")

def test_unknown_or_secret_like_fields_are_rejected(self) -> None:
    payload = self.valid_payload()
    payload["api_key"] = "must-not-be-accepted"
    with self.assertRaisesRegex(ValueError, "api_key"):
        load_enterprise_config(self.write_config(payload))

def test_invalid_values_report_the_field_path(self) -> None:
    cases = (
        ("company.website_url", "ftp://example.com"),
        ("chatwoot.widget_color", "blue"),
        ("company.industry", "finance"),
        ("locale", "en_US"),
        ("assistant.handoff_message", "x" * 501),
    )
    for field_path, invalid in cases:
        with self.subTest(field_path=field_path):
            payload = self.valid_payload()
            target = payload
            parts = field_path.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = invalid
            with self.assertRaisesRegex(ValueError, field_path):
                load_enterprise_config(self.write_config(payload))
```

- [ ] **Step 3: 运行配置测试并确认 RED**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_enterprise_config -v
```

预期：`ModuleNotFoundError: No module named 'app.enterprise_config'`。

- [ ] **Step 4: 实现不可变配置对象和单一校验入口**

`app/enterprise_config.py` 定义冻结 dataclass：

```python
@dataclass(frozen=True)
class CompanyConfig:
    name: str
    industry: Literal["ecommerce"]
    website_url: str

@dataclass(frozen=True)
class AssistantConfig:
    name: str
    welcome_title: str
    welcome_message: str
    handoff_message: str

@dataclass(frozen=True)
class DifyEnterpriseConfig:
    dataset_name: str
    app_name: str
    indexing_technique: Literal["economy", "high_quality"]

@dataclass(frozen=True)
class ChatwootEnterpriseConfig:
    inbox_name: str
    widget_color: str

@dataclass(frozen=True)
class EnterpriseConfig:
    schema_version: int
    company: CompanyConfig
    assistant: AssistantConfig
    dify: DifyEnterpriseConfig
    chatwoot: ChatwootEnterpriseConfig
    locale: Literal["zh_CN"]
    business_timezone: Literal["Asia/Shanghai"]

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)
```

所有映射层用同一组辅助函数校验：

```python
def _require_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object.")
    return value

def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    path: str,
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise ValueError(f"{path}.{missing[0]} is required.")
    if unknown:
        raise ValueError(f"{path}.{unknown[0]} is not allowed.")

def _require_text(value: object, path: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{path} must contain 1-{maximum} characters.")
    return normalized
```

`load_enterprise_config` 以 `utf-8-sig` 读取，捕获 `OSError` 和 `JSONDecodeError`；URL 只接受 `http/https`，颜色匹配 `#[0-9A-Fa-f]{6}`，名称不超过 100 字符，文案不超过 500 字符，未知字段直接失败。

CLI：

```python
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="config/enterprise.example.json")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_enterprise_config(args.path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.print_json:
        print(json.dumps(config.to_mapping(), ensure_ascii=False))
    else:
        print("PASS: Enterprise configuration is valid.")
    return 0
```

- [ ] **Step 5: 补齐配置边界测试并验证 GREEN**

补充文件不存在、非法 JSON、缺字段、错误类型、超长名称、`schema_version != 1` 和 `high_quality` 合法的测试。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_enterprise_config -v
.\.venv\Scripts\python.exe -m app.enterprise_config --path config\enterprise.example.json
```

预期：测试全部通过，CLI 输出 `PASS: Enterprise configuration is valid.`。

- [ ] **Step 6: 提交配置模型**

```powershell
git add config/enterprise.example.json app/enterprise_config.py tests/test_enterprise_config.py .env.example
git commit -m "feat: add validated enterprise configuration"
```

---

### Task 3: 将企业配置接入客服运行时和容器

**Files:**
- Modify: `app/settings.py`
- Modify: `app/customer_service.py`
- Modify: `app/main.py`
- Modify: `tests/test_settings.py`
- Modify: `tests/test_customer_service.py`
- Modify: `Dockerfile`
- Modify: `.dockerignore`
- Modify: `compose.yaml`

**Interfaces:**
- Consumes: Task 2 的 `load_enterprise_config`。
- Produces: `Settings.enterprise_config` 和 `CustomerService(..., handoff_message=...)`。

- [ ] **Step 1: 添加自定义转人工文案失败测试**

在 `tests/test_customer_service.py` 新增：

```python
def test_configured_handoff_message_is_used_for_rules(self) -> None:
    service = CustomerService(
        store=self.store,
        dify_client=self.dify,
        chatwoot_client=self.chatwoot,
        operations_recorder=self.operations,
        monotonic=FakeMonotonic(),
        handoff_message="测试商城正在为你转接人工。",
    )
    result = service.handle(incoming("我要人工客服"))
    self.assertEqual(result.status, "handed_off")
    self.assertEqual(
        self.chatwoot.messages,
        ["测试商城正在为你转接人工。"],
    )
```

在 `tests/test_settings.py` 使用临时企业配置新增：

```python
settings = Settings.from_env({
    **valid_ai_environment(),
    "ENTERPRISE_CONFIG_PATH": str(config_path),
})
self.assertEqual(settings.enterprise_config.company.name, "测试商城")
```

- [ ] **Step 2: 运行聚焦测试并确认 RED**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_customer_service tests.test_settings -v
```

预期：`CustomerService` 不接受 `handoff_message`，`Settings` 没有 `enterprise_config`。

- [ ] **Step 3: 注入配置并保持现有调用兼容**

`CustomerService.__init__` 增加：

```python
handoff_message: str = "正在为你转接人工客服，请稍候。",
```

去除首尾空白后校验 1–500 字符，保存为 `self._handoff_message`，替换规则命中和 Dify 异常两个默认文案使用点。Dify 返回的安全 `[HANDOFF]` 文案仍优先。

`Settings` 增加：

```python
enterprise_config: EnterpriseConfig
```

`Settings.from_env` 读取：

```python
enterprise_config_path = source.get(
    "ENTERPRISE_CONFIG_PATH",
    "config/enterprise.example.json",
).strip()
enterprise_config = load_enterprise_config(enterprise_config_path)
```

`app.main.get_customer_service` 传入：

```python
handoff_message=settings.enterprise_config.assistant.handoff_message,
```

- [ ] **Step 4: 让 Docker 使用同一企业配置**

`.dockerignore` 增加：

```text
!config/
!config/enterprise.example.json
```

`Dockerfile` 增加：

```dockerfile
COPY config/enterprise.example.json ./config/enterprise.example.json
```

`compose.yaml` 增加容器内固定路径和只读挂载：

```yaml
    environment:
      ENTERPRISE_CONFIG_PATH: "/app/config/enterprise.json"
    volumes:
      - "${ENTERPRISE_CONFIG_PATH:-./config/enterprise.example.json}:/app/config/enterprise.json:ro"
      - "./.runtime/customer-service:/app/.runtime/customer-service"
```

- [ ] **Step 5: 运行运行时与容器配置回归**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_customer_service tests.test_settings -v
.\.venv\Scripts\python.exe -m compileall -q app tests
docker compose config | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

预期：聚焦测试通过，编译无输出，Compose 配置有效。

- [ ] **Step 6: 提交运行时配置接入**

```powershell
git add app/settings.py app/customer_service.py app/main.py tests/test_settings.py tests/test_customer_service.py Dockerfile .dockerignore compose.yaml
git commit -m "feat: apply enterprise configuration at runtime"
```

---

### Task 4: 自动创建或复用 Dify 知识库与 Chatflow

**Files:**
- Move: `infra/dify/star-river-customer-service.yml` to `infra/dify/customer-service-template.yml`
- Modify: `scripts/provision-dify-chatflow.ps1`
- Modify: `scripts/provision_dify_chatflow.py`
- Create: `tests/provision-dify-env-test.ps1`
- Modify: `tests/dify-chatflow-dsl-test.ps1`

**Interfaces:**
- Consumes: `-ConfigPath`、`-EnvPath`、通用 Dify DSL 和已初始化的本地 Dify 1.15。
- Produces: `DIFY_DATASET_ID`、`DIFY_DATASET_API_KEY`、`DIFY_INDEXING_TECHNIQUE`、`DIFY_APP_ID` 和 `DIFY_APP_API_KEY`。

- [ ] **Step 1: 把 DSL 改为无企业硬编码的通用模板**

保留现有节点和边，把企业字段改为：

```yaml
app:
  description: __APP_DESCRIPTION__
  name: __DIFY_APP_NAME__
workflow:
  features:
    opening_statement: __OPENING_STATEMENT__
  graph:
    nodes:
    - data:
        dataset_ids:
        - __DIFY_DATASET_ID__
        desc: __KNOWLEDGE_DESCRIPTION__
        type: knowledge-retrieval
    - data:
        prompt_template:
        - role: system
          text: __SYSTEM_PROMPT__
        type: llm
```

其他 DeepSeek 模型、建议问题、节点坐标、边和 `{{#context#}}` 结构保持现状。

- [ ] **Step 2: 更新 DSL 测试并确认 RED**

`tests/dify-chatflow-dsl-test.ps1` 改读通用模板，并断言：

```python
expected = {
    "__APP_DESCRIPTION__",
    "__DIFY_APP_NAME__",
    "__OPENING_STATEMENT__",
    "__DIFY_DATASET_ID__",
    "__KNOWLEDGE_DESCRIPTION__",
    "__SYSTEM_PROMPT__",
}
for placeholder in expected:
    assert placeholder in text, placeholder
assert "星河优选" not in text
```

运行测试，预期在移动模板前失败。

- [ ] **Step 3: 为安全写入 `.env` 建立 fixture 测试**

`tests/provision-dify-env-test.ps1` 创建临时 env、配置和：

```powershell
$fixture = @{
    dataset_id = "11111111-2222-3333-4444-555555555555"
    dataset_api_key = "dataset-fixture-secret"
    app_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    app_api_key = "app-fixture-secret"
}
$fixture | ConvertTo-Json | Set-Content -LiteralPath $fixturePath -Encoding UTF8
$output = & $scriptPath -ConfigPath $configPath -EnvPath $envPath -FixturePath $fixturePath 6>&1 | Out-String
```

断言五个 Dify 配置值各出现一次、无关变量保留、两个 secret 不出现在输出。

- [ ] **Step 4: 扩展 Dify 容器 provisioner**

`scripts/provision_dify_chatflow.py` 新增参数：

```python
parser.add_argument("--dsl", required=True)
parser.add_argument("--enterprise-config", required=True)
parser.add_argument("--secret-output", required=True)
```

Dify 专用 import 放入 `flask_app.app_context()`。固定顺序：

1. 在当前 tenant 查询同名 `Dataset`，不存在时调用 `DatasetService.create_empty_dataset`，permission 为 `only_me`、provider 为 `vendor`。
2. 查询 tenant 最早的 `ApiTokenType.DATASET`，不存在时生成 `ds-` token。
3. 使用 `yaml.safe_load` 设置 app 名、opening statement、知识库 ID、描述和 system prompt，再用 `safe_dump(allow_unicode=True, sort_keys=False)` 输出 DSL。
4. 按配置中的 app 名查询 `App`，导入并发布，复用或创建 app token。
5. 只向权限 0600 的 secret 文件写入：

```python
{
    "dataset_id": str(dataset.id),
    "dataset_api_key": dataset_api_token.token,
    "app_id": str(app_model.id),
    "app_api_key": app_api_token.token,
}
```

system prompt 由固定模板构造，只插入验证后的企业名和人工转接文案，并保留 `{{#context#}}`、禁止编造和 `[HANDOFF]`。

- [ ] **Step 5: 修改 PowerShell provisioner**

参数改为：

```powershell
param(
    [string]$ConfigPath = "config\enterprise.example.json",
    [string]$EnvPath = ".env",
    [string]$FixturePath = ""
)
```

先调用企业配置 CLI。真实模式把 DSL、配置和 provisioner 复制到 Dify API 容器；fixture 模式直接读取 fixture。两种模式共用 `Set-EnvValue` 写入五个 Dify 变量；`finally` 清空 secret 并删除临时文件。

- [ ] **Step 6: 运行脚本和真实 Dify 验证**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\dify-chatflow-dsl-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\provision-dify-env-test.ps1
.\scripts\provision-dify-chatflow.ps1 -ConfigPath config\enterprise.example.json
```

预期：模板和 fixture 测试通过；真实运行复用星河优选知识库和应用，不产生同名重复项。

- [ ] **Step 7: 提交 Dify 自动初始化**

```powershell
git add infra/dify scripts/provision-dify-chatflow.ps1 scripts/provision_dify_chatflow.py tests/provision-dify-env-test.ps1 tests/dify-chatflow-dsl-test.ps1
git commit -m "feat: provision enterprise Dify resources"
```

---

### Task 5: 配置化 Chatwoot 和客户聊天页面

**Files:**
- Modify: `scripts/configure-chatwoot.ps1`
- Modify: `scripts/configure_chatwoot.rb`
- Modify: `scripts/run-widget-demo.ps1`
- Modify: `samples/ecommerce/widget-demo.html`
- Modify: `tests/configure-chatwoot-env-test.ps1`
- Create: `tests/run-widget-demo-test.ps1`

**Interfaces:**
- Consumes: 企业配置、`CHATWOOT_WEBSITE_TOKEN` 和现有 Chatwoot 管理员。
- Produces: 配置化 Website Inbox、Webhook、`ai_handoff` 标签与带项目标记的客户页面。

- [ ] **Step 1: 扩展 Chatwoot fixture 测试并确认 RED**

在 `configure-chatwoot-env-test.ps1` 创建完整企业配置，调用时传 `-ConfigPath`。fixture 增加 `inbox_id = 8`，并断言：

```powershell
"CHATWOOT_INBOX_ID=8"
"ENTERPRISE_CONFIG_PATH=$configPath"
```

预期：当前脚本因不接受 `ConfigPath` 而失败。

- [ ] **Step 2: 让 Ruby provisioner 使用企业配置**

`configure-chatwoot.ps1` 验证配置后，把 JSON 复制到容器临时路径，以第三个参数传给 Rails runner。

`configure_chatwoot.rb` 读取：

```ruby
config = JSON.parse(File.read(ARGV.fetch(2), encoding: 'UTF-8'))
company = config.fetch('company')
assistant = config.fetch('assistant')
chatwoot = config.fetch('chatwoot')
```

使用配置值更新 account、Inbox 和 WebWidget：

```ruby
account.update!(name: company.fetch('name'))
inbox = account.inboxes.find_by(
  name: chatwoot.fetch('inbox_name'),
  channel_type: 'Channel::WebWidget'
)
inbox.channel.update!(
  website_url: company.fetch('website_url'),
  widget_color: chatwoot.fetch('widget_color'),
  welcome_title: assistant.fetch('welcome_title'),
  welcome_tagline: assistant.fetch('welcome_message')
)
inbox.update!(
  greeting_enabled: true,
  greeting_message: assistant.fetch('welcome_message')
)
```

不存在时按相同值创建；Webhook 固定名继续为 `AI Customer Service` 以保证改企业名后更新原记录。输出继续包含 account、inbox、API token、webhook secret 和 website token。

- [ ] **Step 3: 将演示 HTML 改成安全占位模板**

增加：

```html
<meta name="ai-customer-service-demo" content="1">
```

把品牌、介绍、主色和 Chatwoot 地址替换为：

```text
__COMPANY_NAME_HTML__
__ASSISTANT_NAME_HTML__
__WELCOME_MESSAGE_HTML__
__WIDGET_COLOR_CSS__
__CHATWOOT_BASE_URL_JSON__
__CHATWOOT_WEBSITE_TOKEN_JSON__
```

JavaScript 占位符本身不加引号，由 PowerShell 的 `ConvertTo-Json -Compress` 生成合法字符串；HTML 文本使用 `WebUtility.HtmlEncode`。

- [ ] **Step 4: 编写 Widget 渲染和端口行为测试**

`tests/run-widget-demo-test.ps1` 使用临时 env、运行目录和随机高位端口，覆盖：

1. `-RenderOnly` 生成页面，企业名称经过 HTML 编码，Token 不出现在终端输出。
2. 第一次启动后页面返回 200 和项目 meta 标记。
3. 错误 PID 文件不影响根据 HTTP 页面判断“已运行”。
4. 另一个 `python -m http.server` 占用端口时，脚本失败并包含 `occupied by another service`，且该进程仍存活。

测试在 `finally` 中只停止自己启动的进程，确认路径位于系统临时目录后再删除。

- [ ] **Step 5: 实现 Widget 安全渲染和真实状态判断**

`run-widget-demo.ps1` 参数：

```powershell
param(
    [string]$ConfigPath = "config\enterprise.example.json",
    [string]$EnvPath = ".env",
    [string]$RuntimePath = "",
    [int]$Port = 8000,
    [switch]$RenderOnly
)
```

调用配置 CLI 的 `--print-json` 并安全渲染。启动前请求 `http://127.0.0.1:$Port/`：

- 200 且包含项目标记：报告已运行。
- 能连接但无标记：抛出 `Port $Port is occupied by another service.`。
- 连接失败：启动 Python 静态服务器并轮询 20 次。

PID 文件只用于诊断，不作为在线依据，也不终止未知 PID。

- [ ] **Step 6: 运行 Chatwoot 与 Widget 验证**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\configure-chatwoot-env-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\run-widget-demo-test.ps1
.\scripts\configure-chatwoot.ps1 -ConfigPath config\enterprise.example.json
.\scripts\run-widget-demo.ps1 -ConfigPath config\enterprise.example.json
```

预期：脚本测试通过；真实运行复用现有 Inbox，8000 页面显示配置化星河优选内容。

- [ ] **Step 7: 提交 Chatwoot 与 Widget 配置化**

```powershell
git add scripts/configure-chatwoot.ps1 scripts/configure_chatwoot.rb scripts/run-widget-demo.ps1 samples/ecommerce/widget-demo.html tests/configure-chatwoot-env-test.ps1 tests/run-widget-demo-test.ps1
git commit -m "feat: configure enterprise Chatwoot and widget"
```

---

### Task 6: 增加企业一键初始化和本地服务入口

**Files:**
- Create: `scripts/configure-enterprise.ps1`
- Create: `scripts/status-local.ps1`
- Create: `scripts/start-local.ps1`
- Create: `tests/configure-enterprise-test.ps1`
- Create: `tests/status-local-test.ps1`

**Interfaces:**
- Consumes: Task 2–5 的配置 CLI 和幂等配置脚本。
- Produces: `configure-enterprise.ps1`、统一状态检查和一键本地启动。

- [ ] **Step 1: 编写 `-ValidateOnly` 无副作用测试**

`tests/configure-enterprise-test.ps1` 在临时目录创建完整配置和不存在的 env 路径：

```powershell
$output = & $scriptPath -ConfigPath $configPath -EnvPath $envPath -ValidateOnly 6>&1 | Out-String
```

断言输出包含 `PASS: Enterprise configuration and local prerequisites are valid.`，且 env 和运行目录均未创建。再用缺字段配置断言非零退出，错误只含字段路径。

- [ ] **Step 2: 实现企业初始化编排**

参数：

```powershell
param(
    [string]$ConfigPath = "config\enterprise.example.json",
    [string]$EnvPath = ".env",
    [switch]$ValidateOnly
)
```

固定流程：

1. 验证 Python、配置文件、Dify/Chatwoot 脚本和 Docker CLI 存在。
2. 调用配置 CLI；`ValidateOnly` 在此处输出 PASS 并退出，不创建 env。
3. env 不存在时从 `.env.example` 复制。
4. 写入项目相对的 `ENTERPRISE_CONFIG_PATH`。
5. 调用 `provision-dify-chatflow.ps1`。
6. 调用 `configure-chatwoot.ps1`。
7. `OPERATIONS_API_KEY` 为空时调用 `configure-operations.ps1`，已有时复用。
8. 调用 `run-widget-demo.ps1 -RenderOnly`。
9. 输出不含资源 ID 和密钥的完成摘要。

任何子脚本失败立即停止；重复执行依靠各子脚本幂等续接。

- [ ] **Step 3: 编写可注入 URL 的状态测试**

`status-local.ps1` 参数：

```powershell
param(
    [string]$ChatwootUrl = "http://127.0.0.1:3000",
    [string]$DifyUrl = "http://127.0.0.1:8081",
    [string]$AiUrl = "http://127.0.0.1:5001/health",
    [string]$WidgetUrl = "http://127.0.0.1:8000"
)
```

`tests/status-local-test.ps1` 启动临时 HTTP 服务，把四个 URL 指向它，断言四项均为 `ONLINE` 且退出 0；再把一个 URL 指向空闲端口，断言该项 `OFFLINE` 且退出 1。

- [ ] **Step 4: 实现统一启动脚本**

`start-local.ps1` 接受 `ConfigPath`、`EnvPath` 和默认相邻目录 `..\chatwoot-local`，依次：

```powershell
& $runDifyPath start
docker compose --project-directory $chatwootPath up -d
Start-AiServiceIfOffline
& $runWidgetPath -ConfigPath $ConfigPath -EnvPath $EnvPath
& $statusPath
```

`Start-AiServiceIfOffline` 使用隐藏 PowerShell 运行 `run-docling.ps1`，将 stdout、stderr 和 PID 写入 `.runtime/customer-service`，轮询健康接口；只启动离线服务，不重启或停止未知进程。

- [ ] **Step 5: 运行初始化与状态测试**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\configure-enterprise-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\status-local-test.ps1
.\scripts\configure-enterprise.ps1 -ConfigPath config\enterprise.example.json -ValidateOnly
```

预期：两项脚本测试和真实星河优选预检通过，`ValidateOnly` 不修改 env。

- [ ] **Step 6: 提交统一企业入口**

```powershell
git add scripts/configure-enterprise.ps1 scripts/status-local.ps1 scripts/start-local.ps1 tests/configure-enterprise-test.ps1 tests/status-local-test.ps1
git commit -m "feat: add repeatable enterprise setup workflow"
```

---

### Task 7: 更新交付文档并完成全量验收

**Files:**
- Modify: `README.md`
- Create: `docs/enterprise-onboarding.md`
- Modify: `.gitignore` only if新测试产生未忽略的本地产物

**Interfaces:**
- Consumes: 所有前置任务的命令和接口。
- Produces: 第一家电商商家可执行的资料、配置、启动、验收、人工接管和故障恢复手册。

- [ ] **Step 1: 更新 README 主流程**

首选接入流程改为：

```powershell
Copy-Item config\enterprise.example.json config\enterprise.local.json
.\scripts\configure-enterprise.ps1 -ConfigPath config\enterprise.local.json -ValidateOnly
.\scripts\configure-enterprise.ps1 -ConfigPath config\enterprise.local.json
.\scripts\start-local.ps1 -ConfigPath config\enterprise.local.json
```

明确星河优选只是演示企业、每家企业独立部署、`economy` 与 `high_quality` 的选择、所有入口、人工恢复和运营闭环。

- [ ] **Step 2: 编写真实商家接入手册**

`docs/enterprise-onboarding.md` 包含：

- 商家公开信息：品牌、网站、客服名称、欢迎语和转人工文案。
- 知识资料：商品、库存与发货、运费、签收、退换货、退款、发票、活动边界、客服时间和投诉升级。
- 清洗规则：每条政策包含适用条件、数字时效、例外、更新日期和负责人。
- 30–50 条评测题：标准问法、口语化、多轮追问、知识不足、支付安全、投诉和明确转人工。
- 上线验收：RAG、E2E、人工坐席、知识缺口、CSV、备份和密钥轮换。
- 中国内地下一阶段：服务器、域名实名认证与备案、HTTPS、关闭私网 webhook 开关、防火墙、限流、日志告警和备份。

- [ ] **Step 3: 检查版本库没有私密部署数据**

```powershell
git status --short --ignored
git check-ignore -q .env
git check-ignore -q config/enterprise.local.json
git grep -I -E -q 'sk-[A-Za-z0-9]{20,}'
if ($LASTEXITCODE -eq 0) { throw "Potential live API key is tracked." }
```

预期：env、本地企业配置、运行目录、日志和截图均 ignored；跟踪文件没有长格式 `sk-` 密钥。

- [ ] **Step 4: 执行全部自动化回归**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.\.venv\Scripts\python.exe -m compileall -q app tests
.\.venv\Scripts\python.exe -m pip check

$scriptTests = @(
    "configure-dify-env-test.ps1",
    "provision-dify-env-test.ps1",
    "dify-chatflow-dsl-test.ps1",
    "configure-chatwoot-env-test.ps1",
    "configure-operations-env-test.ps1",
    "configure-enterprise-test.ps1",
    "run-widget-demo-test.ps1",
    "status-local-test.ps1"
)
foreach ($test in $scriptTests) {
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\tests\$test"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

预期：Python 全套测试、编译、依赖和全部 PowerShell 测试通过。

- [ ] **Step 5: 执行真实链路、浏览器和 Docker 验收**

```powershell
.\scripts\configure-enterprise.ps1 -ConfigPath config\enterprise.example.json
.\scripts\start-local.ps1 -ConfigPath config\enterprise.example.json
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\rag-evaluation.ps1
1..3 | ForEach-Object {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\customer-service-e2e.ps1
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
.\.venv\Scripts\python.exe .\tests\operations-dashboard-browser.py
docker compose up -d --build docling
docker compose ps
Invoke-RestMethod http://127.0.0.1:5001/health
```

预期：

- RAG 评测保持 8/8。
- 三次 E2E 全部通过。
- 运营看板通过桌面和 360px 手机验收。
- Docker `docling` healthy，健康接口返回 `{"status":"ok"}`。
- 星河优选 Dify 知识库、应用和 Chatwoot Inbox 未出现重复项。

- [ ] **Step 6: 提交交付文档**

```powershell
git add README.md docs/enterprise-onboarding.md .gitignore
git commit -m "docs: add enterprise delivery and validation guide"
```

- [ ] **Step 7: 最终检查功能分支**

```powershell
git status --short
git log --oneline --decorate -10
```

预期：工作区干净；日志包含本计划规定的七个独立提交，没有 env、本地企业配置或运行产物。
