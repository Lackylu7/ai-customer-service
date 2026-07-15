# RAG 客服运营验证闭环设计

## 1. 目标

在现有 Docling、Dify、DeepSeek、Chatwoot 和 Python 编排服务之上增加轻量运营看板，让企业能够量化 AI 客服效果、发现高频知识缺口，并跟踪知识库补充结果。

本阶段继续使用“星河优选”电商客服场景，服务器、域名和公网部署留到后续阶段处理。

### 1.1 成功标准

- 能查看最近 7 天、30 天和全部时间的 AI 客服核心指标。
- 能区分正常回答、主动转人工、风险类转人工、知识不足和系统异常。
- 只有知识不足的问题进入待补知识列表。
- 同一句问题的全半角、大小写和空白差异能够合并累计，避免重复维护。
- 运营人员能把知识缺口标记为待处理、已补充或忽略，并填写备注。
- 能导出安全的 CSV，用于周报和知识维护记录。
- 运营统计故障不能影响客户正常问答与转人工链路。

## 2. 范围

### 2.1 本阶段包含

- AI 编排结果事件采集。
- 指标查询与按日趋势统计。
- 知识缺口聚合、状态维护和备注。
- 运营 API 鉴权。
- 原生 HTML、CSS、JavaScript 运营页面。
- CSV 导出。
- 单元测试、接口测试、浏览器验收和现有回归测试。

### 2.2 本阶段不包含

- 替代 Chatwoot 的坐席绩效、首次响应时长和客服排班报表。
- 替代 Dify 的模型调用日志、Token 消耗和检索详情。
- 多企业租户、RBAC、SSO 和完整后台账号系统。
- 语义聚类、自动生成知识答案或自动发布知识库内容。
- 实时 WebSocket 推送、邮件周报和消息通知。
- 服务器、域名、HTTPS 和公网部署。

## 3. 总体架构

现有客服链路保持不变。Python 编排服务在每次处理 Chatwoot 客户消息后，以最佳努力方式写入运营事件；运营页面读取同一个 SQLite 数据库中的统计结果。

```text
客户消息
   |
   v
Chatwoot webhook
   |
   v
CustomerService
   |-- 正常回答 ----------> service_events: replied
   |-- 主动/风险转人工 ---> service_events: handed_off + reason
   |-- 知识不足 ----------> service_events: handed_off + knowledge_gap
   |                         knowledge_gaps: create or increment
   |-- Dify 异常 ---------> service_events: handed_off + dify_error
   |-- 人工模式消息 ------> service_events: ignored_human_mode
   `-- 重复 webhook ------> service_events: duplicate

SQLite <--- OperationsStore <--- /v1/operations/* <--- /operations
```

Chatwoot 和 Dify 后台继续作为明细来源。运营页面只展示 RAG 业务特有指标，并提供到两个后台的跳转入口。

## 4. 事件语义与指标口径

### 4.1 事件结果

每次 webhook 处理尝试记录一个结果：

- `replied`：Dify 返回可用答案，Chatwoot 已发送 AI 回复。
- `handed_off`：AI 模式下触发人工接管，必须附带原因。
- `ignored_human_mode`：会话已处于人工模式，AI 保持静默。
- `duplicate`：相同 Chatwoot `message_id` 已被处理，未再次调用 Dify 或发送消息。
- `failed`：处理抛出未完成异常，消息 claim 已释放，可由 Chatwoot 重试。

`handed_off` 的原因限定为：

- `explicit_human_request`
- `complaint_or_legal`
- `payment_or_account_security`
- `knowledge_gap`
- `dify_error`

`failed` 的原因限定为：

- `chatwoot_error`
- `internal_error`

### 4.2 指标定义

- **收到的唯一客户消息**：按 `message_id` 取最后一个非 `duplicate` 事件后，统计 `replied + handed_off + ignored_human_mode + failed`。失败后重试成功时，后续成功结果覆盖此前失败结果，避免同一客户问题重复进入业务分母。
- **AI 可处理问题数**：`replied + handed_off`。已进入人工模式的后续消息不进入分母。
- **AI 解答率**：`replied / AI 可处理问题数`。
- **转人工率**：`handed_off / AI 可处理问题数`。
- **知识缺口率**：`reason=knowledge_gap / AI 可处理问题数`。
- **系统异常数**：最终结果中的 `reason=dify_error` 加 `outcome=failed`。
- **重复 webhook 数**：`duplicate` 事件数量，单独展示，不进入业务问题分母。
- **平均 AI 处理耗时**：每个 `message_id` 最终 `replied` 或 `handed_off` 事件的 `duration_ms` 平均值。

分母为零时百分比返回 `0`，不返回 `NaN` 或无穷值。

## 5. 数据模型

继续使用 `CUSTOMER_SERVICE_DB_PATH` 指向的 SQLite 数据库，采用现有连接即用即关和 WAL 模式。初始化使用 `CREATE TABLE IF NOT EXISTS`，不修改现有表字段。

### 5.1 `service_events`

| 字段 | 类型 | 约束与用途 |
| --- | --- | --- |
| `id` | INTEGER | 自增主键 |
| `message_id` | INTEGER | Chatwoot 消息 ID，建立索引 |
| `conversation_id` | INTEGER | Chatwoot 展示会话 ID |
| `outcome` | TEXT | 限定为五种事件结果 |
| `reason` | TEXT NULL | 转人工或失败原因 |
| `duration_ms` | INTEGER | 非负处理耗时 |
| `created_at` | INTEGER | UTC Unix 秒，建立索引 |

事件表不保存正常问题文本、AI 回答、联系人姓名或联系方式。

### 5.2 `knowledge_gaps`

| 字段 | 类型 | 约束与用途 |
| --- | --- | --- |
| `id` | INTEGER | 自增主键 |
| `fingerprint` | TEXT | SHA-256，唯一索引 |
| `sample_question` | TEXT | 脱敏后的代表问题，最多 500 字符 |
| `occurrence_count` | INTEGER | 出现次数，初始为 1 |
| `first_seen_at` | INTEGER | 首次出现 UTC Unix 秒 |
| `last_seen_at` | INTEGER | 最近出现 UTC Unix 秒 |
| `last_conversation_id` | INTEGER | 最近关联的 Chatwoot 会话 |
| `status` | TEXT | `pending`、`resolved`、`ignored` |
| `note` | TEXT | 运营备注，最多 1000 字符 |
| `updated_at` | INTEGER | 最近人工修改时间 |

知识缺口再次出现时，用原子 upsert 增加 `occurrence_count` 并更新最近时间和会话。若已标记 `resolved` 或 `ignored`，重复出现时保留人工状态，避免自动覆盖运营决定。

## 6. 文本脱敏与问题合并

### 6.1 脱敏

知识缺口入库前遮盖：

- 中国大陆手机号。
- 常见电子邮箱。
- 18 位身份证号码。
- 12–19 位连续银行卡或支付卡号码。

脱敏使用可识别占位符，如 `[手机号]`、`[邮箱]`，不保留原值。最终文本去除首尾空白并截断为 500 字符。

### 6.2 指纹

先脱敏，再执行 Unicode NFKC 规范化、转小写、合并连续空白、去除首尾标点，最后计算 SHA-256。

第一版只合并规范化后相同的问题，例如全半角、大小写和多余空格差异；不做语义相似聚类，以避免把不同业务问题错误合并。

## 7. 服务集成

新增独立 `OperationsRecorder` 接口，`CustomerService` 只依赖接口，不直接执行统计 SQL。默认实现写入 `OperationsStore`，测试可使用内存假实现。

记录时机：

- 幂等 claim 失败后记录 `duplicate`。
- 会话已是人工模式时记录 `ignored_human_mode`。
- 规则命中转人工时记录 `handed_off` 和对应规则原因。
- Dify API 失败且成功发送转人工提示时记录 `handed_off/dify_error`。
- Dify 返回 `[HANDOFF]` 时记录 `handed_off/knowledge_gap`，并 upsert 知识缺口。
- AI 回复成功发送后记录 `replied`。
- 未完成异常重新抛出前记录 `failed`，但不改变现有错误响应。

运营写入采用最佳努力原则：统计数据库写入异常只写入不含问题文本和密钥的错误日志，不得阻止客户消息、转人工提示或 webhook 正常响应。

## 8. 运营接口

所有 `/v1/operations/*` 接口必须携带 `X-Operations-Key`。服务端用常量时间比较校验 `OPERATIONS_API_KEY`。

- 未配置管理密钥：HTTP `503`。
- 缺少或错误密钥：HTTP `401`。
- 非法筛选、状态或备注：HTTP `422`。

### 8.1 汇总

`GET /v1/operations/summary?window=7d|30d|all`

返回：

- 核心指标及其明确分子、分母。
- 按日的客户问题、AI 回答、转人工和知识缺口趋势。
- 转人工原因分布。
- 平均处理耗时。

### 8.2 知识缺口列表

`GET /v1/operations/gaps?status=pending|resolved|ignored|all&limit=50&offset=0`

默认按 `occurrence_count DESC, last_seen_at DESC` 排序。`limit` 范围为 1–100。

### 8.3 更新知识缺口

`PATCH /v1/operations/gaps/{gap_id}`

请求体仅允许：

```json
{
  "status": "resolved",
  "note": "已补充退款到账时效说明"
}
```

状态必须为三个允许值之一，备注最多 1000 字符。不存在的 ID 返回 `404`。

### 8.4 CSV 导出

`GET /v1/operations/export.csv?window=7d|30d|all&kind=events|gaps`

响应使用 UTF-8 BOM，便于 Excel 正确显示中文。以 `= + - @`、制表符或回车开头的单元格统一添加单引号，防止 CSV 公式注入。

## 9. 运营页面

`GET /operations` 返回项目内置静态页面，不引入 React、图表库或外部 CDN。

页面包含：

- 顶部服务状态和 Chatwoot、Dify 后台入口。
- 7 天、30 天、全部时间筛选。
- KPI 卡片。
- 原生 SVG 趋势图和转人工原因条形图。
- 可筛选、分页的知识缺口列表。
- 状态选择、备注编辑和保存反馈。
- 当前筛选范围 CSV 导出。
- 手动刷新按钮；第一版不做实时推送。

首次打开时要求输入运营管理密钥。密钥只保存到当前标签页的 `sessionStorage`，通过请求头发送，不进入 URL、页面源代码或日志。所有接口内容使用 DOM `textContent` 渲染，不使用不受信任的 `innerHTML`。

页面必须在 360px 手机宽度和常见桌面宽度下可用，不出现横向遮挡。移动端表格使用卡片式布局。

## 10. 配置

新增：

```text
OPERATIONS_API_KEY=
```

提供 `scripts/configure-operations.ps1`：

- 使用安全随机数生成管理密钥。
- 更新被 Git 忽略的 `.env`。
- 不在终端、参数或日志中输出密钥。
- 重复运行时默认复用已有非空密钥；显式 `-Rotate` 时轮换。

Compose 将 `OPERATIONS_API_KEY` 传入服务容器。未配置时不影响客服与知识入库接口，只禁用运营数据接口。

## 11. 错误处理与兼容性

- 新表创建和查询不能破坏现有 `processed_messages` 与 `conversation_states`。
- 运营记录失败不能改变原客服 `HandlingResult`。
- 不在异常、日志、对象 `repr` 或 HTTP 响应中暴露管理密钥。
- 正常问答内容不进入运营数据库。
- 已有 SQLite 数据无需迁移或清空。
- 现有 Chatwoot、Dify 和网页客服接口保持不变。

## 12. 测试与验收

### 12.1 单元测试

- 事件写入和时间窗口统计。
- 指标分母为零及百分比计算。
- 知识缺口原子 upsert、排序、分页和状态保留。
- 手机、邮箱、身份证、银行卡脱敏。
- Unicode 规范化和指纹稳定性。
- 运营密钥缺失、错误和正确三种情况。
- 状态修改验证和 404。
- CSV 中文编码与公式注入防护。
- 运营写入失败不影响客服主链路。

### 12.2 集成验收

1. 已知退款问题得到 AI 回答，产生 `replied`，不创建知识缺口。
2. 未知保修承诺返回转人工，产生 `handed_off/knowledge_gap` 和一条 `pending` 缺口。
3. 规范化后相同的未知问题再次出现，只增加同一缺口的次数。
4. “我要转人工”产生 `explicit_human_request`，不创建知识缺口。
5. 支付安全问题产生 `payment_or_account_security`，不创建知识缺口。
6. Dify 异常产生 `dify_error`，不创建知识缺口。
7. 人工模式后续消息产生 `ignored_human_mode`，AI 不回复。
8. 重复 webhook 产生 `duplicate`，不重复调用 Dify 或发送回复。

### 12.3 回归与浏览器验收

- 现有 Python 单元测试全部通过。
- RAG 评测保持 8/8。
- Chatwoot 客服 E2E 保持通过。
- Docker 镜像继续构建并通过 `/health`。
- 浏览器验证桌面与手机布局、筛选、编辑、导出和错误密钥提示。

## 13. 后续阶段

运营闭环完成并积累真实问题后，再进入生产安全和公网部署阶段：服务器、域名、HTTPS、共享数据库、备份、日志告警、上传鉴权和多副本运行。
