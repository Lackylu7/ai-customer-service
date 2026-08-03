# AI 智能客服

这是一个已通过本地真实组件链路与自动化验收、尚待真实商家灰度验证的单企业 RAG 客服交付模板：企业资料经 Docling 整理后进入 Dify 知识库，访客通过 Chatwoot 咨询，自研编排服务把消息写入耐久队列，由后台 Worker 调用 Dify Chatflow 与 DeepSeek 回答；资料不足、投诉、支付安全或用户主动要求时转人工。复制企业配置即可为不同商家独立部署，企业之间不共享数据和密钥。

真实商家知识接入见 [docs/knowledge-onboarding.md](docs/knowledge-onboarding.md)，单服务器上线、HTTPS、备份和恢复见 [docs/production-deployment.md](docs/production-deployment.md)。

## 每个组件负责什么

```text
企业资料 -> Docling -> Markdown -> Dify 知识库
访客网页 -> Chatwoot -> Webhook API -> SQLite 队列 -> Worker -> Dify RAG -> DeepSeek
                                                        \-> ai_handoff -> Chatwoot 人工坐席
```

- **Docling**：把 PDF、Word、HTML 等资料解析为结构清晰的 Markdown，不负责回答问题。
- **Dify**：保存知识库、检索相关片段，并运行客服 Chatflow。
- **DeepSeek**：根据检索上下文生成自然语言答案。
- **Chatwoot**：提供网页聊天窗口、会话工作台、人工客服和标签管理；它是客服前台，不等于本项目的 RAG 大脑。
- **本项目的 Python API**：验证 Chatwoot webhook 并在一秒内耐久入队，不在请求线程等待大模型。
- **本项目的 Worker**：按会话顺序调用 Dify、持久化待发送动作、幂等发送回复，并控制 AI/人工模式切换。

## 本地完整启动流程

需要 Windows PowerShell、Python 3.12、Docker Desktop，以及已初始化的本地 Chatwoot。所有真实密钥只放在已被忽略的 `.env` 中，不要写入代码、文档、命令参数或聊天记录。

### 1. 安装 Python 环境

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-docling.txt
.\scripts\prepare-docling-model-image.ps1
```

模型准备脚本只下载本服务实际使用的版面、精确表格和中文 Torch OCR 模型，逐个校验大小与 SHA-256 后导入本地模型镜像。已验证的模型会直接复用；首次执行受网络影响可能较慢。

### 2. 启动 Dify

```powershell
.\scripts\setup-dify.ps1
.\scripts\run-dify.ps1 start
```

Dify 控制台位于 <http://127.0.0.1:8081>。首次使用需要创建管理员，并在模型供应商页面配置 DeepSeek 的 `deepseek-chat` 凭证。

如果拉取镜像超时，可先执行：

```powershell
.\scripts\pull-dify-images.ps1
.\scripts\run-dify.ps1 start
```

### 3. 配置和导入知识库

先在 Dify 创建空知识库，再从“知识库设置 → 后端服务 API”创建知识库 API Key。配置脚本会安全读取密钥并写入 `.env`：

```powershell
.\scripts\configure-dify-knowledge.ps1 -DatasetId "知识库 UUID"
.\scripts\run-docling.ps1
```

知识上传接口受 `OPERATIONS_API_KEY` 保护。推荐在运营看板的“导入企业知识资料”区域选择文件；页面会逐个调用 Docling 整理并提交到当前 Dify 知识库，不需要把 Dify 密钥交给浏览器。

DeepSeek 不提供 embedding API，因此演示环境使用 Dify `economy` 关键词索引；生产项目可接入独立 embedding 和 reranker 后改为 `high_quality`。

### 4. 发布 Dify 客服 Chatflow

```powershell
.\scripts\provision-dify-chatflow.ps1
```

脚本会按企业配置导入并发布客服应用，绑定当前知识库，复用或创建应用级 API Key，然后安全写入 `.env`。重复执行不会创建重复应用。

### 5. 启动并配置 Chatwoot

本项目当前使用相邻目录的本地 Chatwoot：

```powershell
Push-Location ..\chatwoot-local
docker compose up -d
Pop-Location
.\scripts\configure-chatwoot.ps1
```

配置脚本会复用或创建 Website Inbox、`message_created` webhook 和 `ai_handoff` 标签，并把服务端配置安全写入 `.env`。

Chatwoot 默认会阻止向私网地址发送 webhook。本地开发的 `chatwoot-local/docker-compose.yaml` 已为 Rails 和 Sidekiq 设置 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`，使签名 webhook 可以访问 `host.docker.internal:5001`。这个开关只适合可信的本地环境，生产环境必须关闭并改用公开 HTTPS webhook 地址。

### 6. 启动 AI 服务和网页演示

配置更新后需要重启 Python API 和 Worker，使二者重新加载 `.env`。两个进程缺一不可，请分别打开两个终端：

```powershell
# 终端 1：Webhook API、文档接口和运营看板
.\scripts\run-docling.ps1

# 终端 2：Dify 生成、Chatwoot 交付和漏消息对账
.\scripts\run-worker.ps1
```

Webhook 对新消息返回 `202 {"status":"queued"}`，表示消息已经安全写入本地 SQLite 队列，不表示回答已经生成。重复消息返回 `200 {"status":"duplicate"}`。API 单独运行时仍会继续接收并积压消息，只有 Worker 运行后才会生成和发送回答；运营人员应通过 `/ready` 和看板及时发现 Worker 停止或队列积压。

另开终端启动演示网页：

```powershell
.\scripts\run-widget-demo.ps1
```

打开 <http://127.0.0.1:8000>，可以测试：

- `现货下单后多久发货？`：知识库回答。
- `退款一般多久到账？`：多轮 RAG 回答。
- `我要转人工客服`：回复转接提示、添加 `ai_handoff` 标签并停止 AI。

Chatwoot 坐席工作台位于 <http://127.0.0.1:3000>。页面显示“离线”只代表当前没有在线人工坐席，不影响 webhook 机器人自动回复。

### 7. 配置并使用运营看板

首次使用时生成运营管理密钥，然后重启 API 和 Worker 加载配置：

```powershell
.\scripts\configure-operations.ps1
.\scripts\run-docling.ps1
.\scripts\run-worker.ps1
```

打开 <http://127.0.0.1:5001/operations>，从本地 `.env` 复制 `OPERATIONS_API_KEY` 到密钥弹窗。密钥只保存在当前标签页的 `sessionStorage`，不会进入 URL；关闭标签页后自动清除。需要轮换时执行 `configure-operations.ps1 -Rotate`，然后重启 AI 服务。

看板提供近 7 天、近 30 天和全部时间三个范围，展示唯一客户消息、AI 解答率、转人工率、知识缺口率、系统异常和平均处理耗时。顶部受保护的队列面板还显示等待/处理中数量、最老等待、失败任务、Worker 心跳和 Dify/Chatwoot 最近状态。Dify 返回 `[HANDOFF]` 的未知问题会进入知识缺口队列，可标记为“待处理”“已补充”或“已忽略”并填写维护备注；事件和缺口均可导出为适合 Excel 的安全 CSV。

看板的“人工接管会话”区域按 SLA 显示等待中、即将超时和已超时状态。管理员处理完会话后，点击“恢复 AI 接管”并二次确认即可恢复；不需要在服务器运行命令。若接口返回 HTTP 409，表示该会话仍有生成中、投递中、重试中或失败任务，应先处理任务再重试。若 Chatwoot 标签操作失败，本地会话会继续保持人工模式，不会误切回 AI。

`GET /health` 只表示 API 进程存活；`GET /ready` 只有在配置有效、SQLite 可写且 Worker 最近 45 秒内有心跳时才返回 200。反向代理和容器编排应使用 `/ready` 判断整条客服处理链路是否可用。

运营写入采用 best-effort：即使统计数据库暂时不可用，也不会阻止正常 AI 回复或转人工。Chatwoot 后台继续负责坐席绩效、会话和人工接管明细，Dify 后台继续负责模型调用、Token 和检索明细，本看板只聚焦 RAG 业务效果与知识维护闭环。

## Docker 运行编排服务

Python 服务也可以容器化运行：

```powershell
.\scripts\prepare-docling-model-image.ps1
docker compose up -d --build docling worker
docker compose ps
```

Compose 会让 API 和 Worker 共享 `.runtime/customer-service` SQLite 状态目录，并让容器通过 `host.docker.internal` 访问本机 Dify 和 Chatwoot。只有 API 端口绑定 `127.0.0.1`，Worker 不发布端口。人工 SLA 与托底冷却时间也由项目 `.env` 传给 API 和 Worker。容器模式下 Chatwoot webhook 仍使用：

Dockerfile 会从 PyTorch 官方 CPU 索引安装 Torch，并从通过 SHA-256 校验的本地模型镜像复制 Docling 版面、表格和中文 OCR 模型；运行时不需要联网下载。生产覆盖配置还会让 API/Worker 使用非 root 用户、只读根文件系统和最小 Linux 权限。

```text
http://host.docker.internal:5001/v1/chatwoot/webhook
```

## 测试与验收

单元测试、编译检查和依赖检查：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.\.venv\Scripts\python.exe -m compileall -q app tests
.\.venv\Scripts\python.exe -m pip check
```

配置、Dify DSL 和真实端到端验收：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\configure-dify-env-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\dify-chatflow-dsl-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\configure-chatwoot-env-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\configure-operations-env-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\compose-worker-config-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\rag-evaluation.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\customer-service-e2e.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\chatwoot-real-webhook-e2e.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\human-handoff-e2e.ps1
python .\tests\operations-dashboard-browser.py
```

直接 E2E 脚本会创建临时 Chatwoot 联系人与会话，发送正确签名的 webhook，并轮询验证：异步知识库回复仅出现一次、重复事件被忽略、人工转接标签生效、进入人工模式后 AI 保持静默、运营指标入库、知识缺口状态与备注可更新。真实 Webhook E2E 则从 Chatwoot Website Inbox 的网站挂件访客 API 发起客户消息，不直接调用本项目 Webhook，用于验证 Chatwoot → Webhook → 队列 → Worker → Dify → Chatwoot 全链路。脚本不会打印任何真实密钥。浏览器脚本使用 Python Playwright 和本机 Microsoft Edge，验证错误密钥、队列健康、看板加载、筛选、CSV 下载以及桌面和 360px 移动布局。

Docling 冒烟测试：

```powershell
.\tests\docling-smoke.ps1
.\.venv\Scripts\python.exe .\tests\docling-pdf-smoke.py
```

## 人工接管、超时托底与恢复 AI

默认流程如下：

```text
客户触发转人工
  -> AI 添加 ai_handoff 并停止普通回答
  -> 5 分钟内等待真人客服公开回复
     -> 真人已公开回复：取消尚未发送的托底，继续由人工接管
     -> 无真人公开回复：仅发送一次安全托底
        -> 普通知识问题：发送带“AI 临时协助”标识的 RAG 答案
        -> 投诉、支付、账户安全、知识不足或 Dify 故障：只提示继续等待人工
  -> 管理员在 /operations 二次确认“恢复 AI 接管”
  -> 只有客户下一条新消息进入 RAG，恢复前的旧消息不会补答
```

“AI 临时协助”只是人工繁忙时的托底，不会改变会话的人工所有权，也不会清除 `ai_handoff` 标签。Chatwoot 坐席的公开回复会取消尚未发送的托底；私密备注不会被当作已回复客户。托底发送后仍保持人工模式，并受同会话冷却时间限制，避免重复刷屏。

相关配置均从项目 `.env` 读取，修改后需重启 API 和 Worker：

| 配置 | 默认值 | 允许范围 | 生产建议 |
| --- | ---: | ---: | --- |
| `CUSTOMER_SERVICE_HUMAN_SLA_SECONDS` | 300 秒 | 1..86400 秒 | 按坐席承诺设置为 300..900 秒 |
| `CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS` | 300 秒 | 1..86400 秒 | 不小于 SLA，避免连续托底 |

正常恢复应在 <http://127.0.0.1:5001/operations> 操作。`reset-ai-conversation.ps1` 仅保留为看板或控制 API 不可用时的本机应急工具；使用前应先停止 Worker 并确认没有投递中或失败任务：

```powershell
.\scripts\reset-ai-conversation.ps1 -ConversationId 42
```

应急脚本只删除 `ai_handoff` 标签，保留其他标签，并清空该会话的 Dify 上下文。恢复后重新启动 Worker，并只用客户的新消息验证 AI；不要期待系统补答人工期间的旧消息。

## 失败任务恢复

Chatwoot 鉴权失败或交付重试耗尽后，任务会进入 `failed`，并只阻塞同一会话的后续消息。先修复 `.env` 中的凭证并重启 Worker，再按看板或日志中的入站消息 ID 在本机执行：

```powershell
.\scripts\retry-customer-service-job.ps1 -MessageId 123456
```

成功后脚本只输出任务已重新排队。不要把数据库、密钥或完整客户消息复制到命令行、工单或聊天记录中。

## 正式上线还需要现场完成

代码侧已经具备生产 Compose、双域名自动 HTTPS、Host 校验、安全响应头、受保护的知识上传、离线 Docling 模型、容器最小权限、健康检查、加密备份和强确认恢复。服务器现场仍需完成：

- 核对服务器规格、Linux/SSH 和数据盘，安装 Docker 与备份工具。
- 把两个域名解析到服务器，仅开放 SSH、80 和 443，配置 Chatwoot SMTP。
- 在服务器初始化 Dify/Chatwoot，写入新生成的生产密钥并关闭本地私网 webhook 开关。
- 导入真实商家资料，完成检索评测、全链路 E2E、3—7 天灰度和人工客服培训。
- 配置异地备份同步、定时健康告警和密钥轮换；任何曾暴露在聊天或日志中的密钥必须作废重建。
