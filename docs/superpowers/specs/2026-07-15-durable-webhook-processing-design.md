# AI 客服持久化消息处理设计

## 1. 目标

把当前同步执行的 Chatwoot webhook 改造成适合单台服务器、单企业灰度试用的持久化异步处理链路。Chatwoot 请求完成验签和持久化入队后立即返回，Dify、DeepSeek、Chatwoot 回写和转人工操作由独立 Worker 完成。

本阶段的成功标准是：大模型响应速度不再影响 webhook 响应；重复投递不会产生重复任务或重复回复；服务重启后未完成消息可恢复；同一会话保持顺序，不同会话可以并行；Chatwoot 短暂故障不会导致重新调用大模型或静默丢失消息。

## 2. 已确认约束

- 第一版只面向单台服务器和首家真实电商商家的灰度试用。
- 使用 SQLite 持久化任务队列，不在本阶段引入 Redis、Celery、RQ 或 PostgreSQL。
- 默认允许两个任务并行处理，但同一 Chatwoot 会话始终串行。
- 保留现有 Docling、Dify、DeepSeek、Chatwoot、运营看板和知识缺口能力。
- 设计清晰的队列存储接口，未来迁移 PostgreSQL 时不修改客服业务规则。

## 3. 范围

### 3.1 本阶段包含

- Webhook 快速验签、解析、幂等入队和快速响应。
- SQLite 持久化任务表、原子领取、任务租约、重试和失败状态。
- 独立 Worker 进程和默认两路并发。
- AI 结果先持久化、再发送 Chatwoot 的分阶段处理。
- Chatwoot 回复 `source_id` 幂等和发送结果核对。
- 转人工回复、标签和本地会话状态的一致性恢复。
- Chatwoot 入站消息定期对账，补回服务停机期间漏掉的 webhook。
- 队列指标、Worker 心跳、就绪检查和对应自动化测试。
- 本地 PowerShell 启动方式和 Docker Compose Worker 服务。

### 3.2 本阶段不包含

- 多服务器、多副本、高可用和自动扩缩容。
- Redis、PostgreSQL 或其他共享队列迁移。
- 知识上传与文档转换接口的身份认证、限流和恶意文件扫描。
- DeepSeek、Chatwoot 和 Dify 的日志脱敏与集中密钥管理。
- 企业通用配置、一键初始化、中国内地服务器、域名和 HTTPS。

这些未包含项目仍属于审查整改清单，按优先级在本阶段完成后继续处理。

## 4. 总体架构

```text
Chatwoot
   |
   | signed webhook
   v
FastAPI webhook
   |-- 验签、校验账号、解析入站文本
   |-- INSERT OR IGNORE 到 SQLite 任务队列
   `-- 立即返回 queued / duplicate / ignored

SQLite
   |-- inbound_jobs
   |-- conversation_states
   |-- processed_messages
   |-- worker_heartbeats
   `-- reconciliation_state

Worker supervisor（默认并发 2）
   |-- 原子领取可执行任务
   |-- 调用 Dify / DeepSeek
   |-- 持久化生成结果
   |-- 幂等回写 Chatwoot
   |-- 同步转人工标签和本地状态
   `-- 写入运营事件与最终状态

Reconciler
   `-- 定期从 Chatwoot 补录漏掉的入站消息
```

API 进程只负责接收和持久化消息，不执行大模型或 Chatwoot 出站调用。Worker 与 API 使用相同应用镜像和同一个 SQLite 持久化卷，但运行不同命令。

## 5. Webhook 接收流程

1. 限制请求体不超过现有的 256 KiB。
2. 使用 Chatwoot 时间戳和 HMAC 签名验证原始请求体。
3. 解析 `message_created`、`incoming`、非私密、文本类型消息。
4. 校验 `account_id` 与当前部署实例一致。
5. 仅保存规范化后的消息字段，不保存完整 webhook 原文或签名：
   - `message_id`
   - `account_id`
   - `conversation_id`
   - `sender_id`
   - `content`
   - 当前标签快照
6. 使用 Chatwoot `message_id` 唯一约束执行幂等入队。

响应约定：

- 新任务持久化成功：HTTP 202，`{"status":"queued"}`。
- 消息已存在：HTTP 200，`{"status":"duplicate"}`。
- 非目标事件或其他账号：HTTP 200，`{"status":"ignored"}`。
- 签名错误：HTTP 401。
- 请求体或 JSON 无效：HTTP 422。
- SQLite 无法持久化：HTTP 503，同时记录不含消息正文和密钥的错误日志。

Webhook 的自动化验收目标是：即使模拟 Dify 阻塞 60 秒，入队响应仍在 1 秒内完成。

## 6. 持久化任务模型

新增 `inbound_jobs` 表，主要字段如下：

| 字段 | 用途 |
| --- | --- |
| `id` | 本地单调递增任务序号，用于会话内排序 |
| `message_id` | Chatwoot 消息 ID，唯一幂等键 |
| `account_id`、`conversation_id`、`sender_id` | 业务关联 ID |
| `content`、`labels_json` | 规范化入站内容和标签快照 |
| `stage` | 当前处理阶段 |
| `resume_stage` | `retry_wait` 结束后恢复到生成或交付阶段 |
| `decision`、`reply_text`、`reason` | 已持久化的回复或转人工决定 |
| `dify_conversation_id` | Dify 返回的会话 ID |
| `outgoing_source_id` | Chatwoot 幂等出站标识 |
| `generation_attempts`、`delivery_attempts` | 分阶段重试次数 |
| `available_at` | 下次允许领取时间 |
| `locked_at`、`locked_by` | Worker 任务租约 |
| `last_error_code` | 脱敏后的稳定错误分类，不保存远端响应全文 |
| `created_at`、`updated_at` | Unix 时间戳 |

任务阶段为：

```text
queued
  -> generating
  -> ready_to_deliver
  -> delivering
  -> completed

任一可重试阶段 -> retry_wait -> 原阶段
不可恢复或超过重试次数 -> failed
```

状态改变、结果写入、重试计数和租约释放必须在 SQLite 事务内完成。现有 `processed_messages` 表继续作为已完成消息的兼容记录，迁移过程不删除已有数据。

Webhook 入队后不再调用现有同步 `CustomerService.handle` 路径。Worker 使用分阶段处理服务，并在任务完成时把 `processed_messages` 对应记录写为 `completed`；旧记录仍参与重复消息判断，避免升级后重新处理已经完成的消息。

## 7. 任务领取、并发和顺序

- Worker supervisor 默认启动两个执行线程，每个线程使用独立 SQLite 连接。
- 任务领取使用短事务和 `BEGIN IMMEDIATE`，确保同一任务不会被两个 Worker 同时领取。
- 只有当同一 `conversation_id` 不存在序号更小的未完成任务时，当前任务才可领取。
- 不同会话可以被不同 Worker 同时处理。
- 任务领取后获得带 `locked_by` 的租约。正常处理结束后释放租约。
- 处理进程异常退出时，超过租约期限的任务重新进入可领取状态。
- 默认租约必须长于 Dify 与 Chatwoot 的总超时时间；初始值为 5 分钟，可通过环境变量调整。

终止状态 `failed` 不静默删除。它会进入运营异常统计并保留任务信息，供人工处理和后续重放。失败任务视为会话阻塞项，在人工处理前不允许同一会话的后续消息越过它，避免对话顺序和上下文错误。新增仅供本机管理员使用的 `scripts/retry-customer-service-job.ps1 -MessageId <id>`，在确认依赖恢复后把指定失败任务原子地放回正确阶段；脚本不得打印消息正文、密钥或远端错误原文。

## 8. 分阶段生成与交付

### 8.1 生成阶段

1. 读取当前本地会话状态。
2. 已是人工模式时，生成 `ignored_human_mode` 决定，不调用 Dify。
3. 命中本地转人工规则时，生成转人工决定和客户提示，不调用 Dify。
4. 其他消息调用 Dify；将回答、Dify 会话 ID、转人工决定、原因和运营分类一次性写入任务。
5. 只有生成结果持久化成功后，任务才能进入 `ready_to_deliver`。

Dify 临时错误最多重试两次；仍失败时生成 `dify_error` 转人工决定。生成结果已经持久化后，任何 Chatwoot 交付重试都不得再次调用 Dify。

### 8.2 Chatwoot 交付阶段

每条客户可见回复使用稳定标识：

```text
ai-cs:<incoming_message_id>:reply
```

该值作为 Chatwoot 消息的 `source_id`。首次发送前正常创建出站消息；任务从 `delivering` 恢复或发生不确定错误时，先读取当前会话消息并查找相同 `source_id`：

- 已存在：把该发送步骤视为成功，不再发送。
- 不存在：重新发送相同结果。

发送成功后记录 Chatwoot 返回的消息 ID。这样可以覆盖“Chatwoot 已创建回复，但 Worker 在写回本地状态前退出”的崩溃窗口。

### 8.3 转人工一致性

转人工任务按以下顺序执行：

1. 幂等发送客户提示。
2. 在保留已有标签的前提下添加 `ai_handoff`。
3. 把本地 `conversation_states.mode` 更新为 `human`，并清除 Dify 会话 ID。
4. 标记任务完成并写入运营事件。

标签写入失败时不得吞掉异常，也不得把任务标记完成。因为同一会话的后续任务被当前任务阻塞，所以重试期间 AI 不会继续回答该会话。人工模式恢复为 AI 仍沿用现有显式重置流程，本阶段不改变坐席操作规则。

## 9. 重试和失败分类

建议初始退避间隔为 5 秒、30 秒、2 分钟和 10 分钟，并加入小幅随机抖动。

- Dify 网络错误、限流和 5xx：生成阶段重试，最多两次，之后转人工。
- Dify 401 或 403：不做无意义重试，记录配置异常并生成转人工决定。
- Chatwoot 网络错误、限流和 5xx：交付阶段重试，最多五次。
- Chatwoot 401 或 403：进入 `failed` 并阻塞当前会话，等待管理员修复凭证后重放。
- SQLite 短暂锁冲突：短间隔重试，不计入远端服务失败次数。
- 签名、请求格式和业务校验错误：不入队，不重试。
- 数据约束错误、不可解析的已持久化任务或重试耗尽：进入 `failed`。

`last_error_code` 只记录稳定分类，例如 `dify_timeout`、`chatwoot_unreachable`、`label_update_failed` 和 `invalid_job_data`，不得保存 API Key、签名、请求头、完整远端响应或用户完整消息。

## 10. Chatwoot 对账恢复

Chatwoot 本身保存全部访客消息，因此它是漏消息恢复的事实来源。

- 新增 `reconciliation_state`，保存上次成功对账时间。
- 首次升级时把游标初始化为当前时间，避免重新处理历史消息。
- 之后 Worker 启动时立即执行一次对账，并按默认 60 秒间隔持续执行。
- 每次读取游标之后一段带重叠窗口的 Chatwoot 会话和消息，重叠数据依靠 `message_id` 唯一键去重。
- 只有完整完成本轮分页读取后才推进游标。
- Chatwoot 暂时不可用时保留旧游标，下次继续。
- 运维可传入显式回溯时间执行一次人工补录，但默认启动不得扫描升级前的全部历史记录。

对账得到的消息必须复用与 webhook 完全相同的解析和入队服务，不能形成第二套业务规则。

## 11. 运行入口与配置

新增配置建议：

```text
CUSTOMER_SERVICE_WORKER_CONCURRENCY=2
CUSTOMER_SERVICE_JOB_LEASE_SECONDS=300
CUSTOMER_SERVICE_RECONCILE_INTERVAL_SECONDS=60
CUSTOMER_SERVICE_GENERATION_MAX_ATTEMPTS=2
CUSTOMER_SERVICE_DELIVERY_MAX_ATTEMPTS=5
```

- 新增 `python -m app.worker` 作为 Worker 标准入口。
- 新增 `scripts/run-worker.ps1`，沿用现有 `.env` 安全读取方式。
- Docker Compose 新增 `worker` 服务，与 API 使用相同镜像、环境变量和 SQLite 持久化卷，不开放端口。
- API 和 Worker 的运行文档必须明确说明二者缺一不可。

## 12. 健康检查和运营可见性

- `/health` 只表示 API 进程存活，保持轻量。
- 新增 `/ready`，检查配置、SQLite 可写性和最近 Worker 心跳；任一失败时返回非 2xx。
- Dify、Chatwoot 最近探测状态和以下队列指标通过受保护的运营接口展示：
  - 等待任务数
  - 正在处理任务数
  - 最老等待任务时长
  - 重试等待任务数
  - 失败任务数
  - Worker 最后心跳时间
- Worker 定期写入 `worker_heartbeats`，停止后不得继续显示为健康。

本阶段只提供观察和失败识别，不新增通用告警平台。集中日志和外部告警属于后续生产部署整改。

## 13. 测试设计

### 13.1 队列与状态单元测试

- 相同 `message_id` 只能入队一次。
- 原子领取不会把同一任务分配给两个 Worker。
- 同一会话的后续任务不会越过前序未完成任务。
- 不同会话可以同时领取。
- 过期租约可以恢复，未过期租约不能被抢占。
- 各阶段状态、结果、重试和失败转换符合约束。

### 13.2 Webhook 测试

- 正确签名的新消息返回 202 和 `queued`。
- 重复消息返回 200 和 `duplicate`。
- 非目标事件返回 `ignored`。
- 慢速或阻塞的伪 Dify 客户端不会被 webhook 调用，响应时间小于 1 秒。
- SQLite 入队失败返回 503，错误响应不泄露消息正文和密钥。

### 13.3 Worker 与恢复测试

- 生成结果持久化后，交付重试不会再次调用 Dify。
- 模拟发送成功后进程退出，恢复时通过 `source_id` 识别已有回复。
- 标签失败后继续重试，且不会重复发送转人工提示。
- 任务失败和 Worker 心跳正确反映在运营指标与就绪检查中。
- 对账能够补录 webhook 未到达的消息，重叠窗口不会重复入队。

### 13.4 端到端验收

- 保留现有直接签名 E2E 作为服务边界测试，但更新预期状态为 `queued`。
- 新增由 Chatwoot 真实创建入站消息触发的 E2E，不再只手工构造 webhook。
- 验证已知问题回复、知识缺口、转人工、人工模式静默和运营事件。
- 连续运行三次，确认不存在随机知识缺口匹配误报。
- 运行 83 个现有单元测试、配置测试、RAG 评测和运营看板浏览器测试，确保全部回归通过。

## 14. 验收标准

- 模拟大模型响应 60 秒时，Chatwoot webhook 在 1 秒内完成入队响应。
- 同一消息重复投递十次只产生一个任务和一条客户可见回复。
- 两个不同会话可并行处理；同一会话回复顺序与入站顺序一致。
- 在生成完成、发送前后和标签更新前后模拟进程退出，重启后任务均可恢复且不重复回复。
- API 停止期间产生的 Chatwoot 消息在恢复后通过对账进入队列。
- `failed` 任务和停止心跳的 Worker 能被运营接口识别。
- 本地四个入口恢复正常，版本库没有新增密钥、运行数据库或测试产物。

## 15. 后续整改顺序

本设计完成后，继续按审查优先级处理：

1. 隔离并保护知识上传与文档转换接口，增加认证、大小限制、类型白名单和限流。
2. 处理 Chatwoot 作业日志密钥暴露，轮换曾进入聊天或日志的密钥。
3. 实施可重复交付的企业配置和一键初始化计划。
4. 补齐依赖锁定、CI、备份、数据保留和真实生成质量评测。
5. 接入第一家商家资料后，再进行中国内地服务器、域名、HTTPS 和灰度上线。
