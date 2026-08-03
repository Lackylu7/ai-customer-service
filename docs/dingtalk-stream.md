# 钉钉内部服务助手运行说明

当前钉钉机器人使用官方 Python Stream SDK 接收消息。Dify 继续负责知识检索；
股东制 3.0 问题可以在检索到已审核证据后，交给同机 OpenClaw 独立 Agent
组织表达。其他业务域默认保持原 Dify 路径。

## 处理链路

```text
员工
  -> 钉钉 Stream
  -> DingTalkEnterpriseHandler
  -> DingTalkAssistantAdapter
  -> 安全策略与敏感信息拦截
  -> 门店实时查询（命中安全白名单时）
     或 Dify RAG / DeepSeek
       -> 股东制 3.0 已审核证据
       -> OpenClaw shareholder-3-0 Agent（可选）
       -> 本地数字与输出校验
  -> 钉钉会话回复
```

OpenClaw 不绑定钉钉通道，调用时不使用 `--deliver`。钉钉收发始终由现有
Python Stream worker 负责，避免与生产机器人抢连接。

Stream 回调提供员工 ID、会话 ID和消息 ID。适配器使用
`员工 ID + 钉钉会话 ID` 隔离 Dify conversation，并将状态保存在 SQLite，
从而支持连续追问、进程重启后续接和重复消息去重。

## 本地启动

先启动 Docker Desktop 和 Dify，再运行：

```powershell
.\scripts\run-dify.ps1 start
.\scripts\start-dingtalk-native-stream.ps1 `
  -UnifiedAppId "<unified-app-id>" `
  -AllowedUserId "<staff-id-1>,<staff-id-2>" `
  -HandlerUserId "<handler-staff-id>"
```

启动脚本通过 DWS 凭据存储读取应用 AppKey 和 AppSecret，仅在当前进程内
设置环境变量，不打印或写入源码。不要同时运行旧的
`start-dingtalk-stream.ps1`，否则同一机器人存在两个 Stream 连接时可能随机
分流消息。

## 生产配置

生产环境应通过密钥管理服务注入下列变量：

- `DINGTALK_CLIENT_ID`
- `DINGTALK_CLIENT_SECRET`
- `DINGTALK_ALLOWED_USERS`
- `DINGTALK_HANDLER_USERS`
- `DINGTALK_OPERATOR_USERS`
- `DINGTALK_DOMAIN_HANDLERS`
- `DINGTALK_ROBOT_CODE`
- `DINGTALK_STATE_DB_PATH`
- `DINGTALK_AUTO_GROUP_ENABLED`
- `DINGTALK_DWS_EXECUTABLE`
- `DINGTALK_DWS_PROFILE`
- `DINGTALK_COLLABORATION_GROUP_PREFIX`
- `DINGTALK_STORE_CONFIG_PATH`
- `DINGTALK_STORE_PROVIDER=openapi`
- `DINGTALK_STORE_OPERATOR_ID`
- `DIFY_BASE_URL`
- `DIFY_APP_API_KEY`
- `OPENCLAW_SHAREHOLDER_ENABLED`
- `OPENCLAW_EXECUTABLE`
- `OPENCLAW_SHAREHOLDER_AGENT_ID`
- `OPENCLAW_SHAREHOLDER_THINKING`
- `OPENCLAW_SHAREHOLDER_TIMEOUT_SECONDS`

`DINGTALK_ALLOWED_USERS` 是逗号分隔的员工 staffId。该值不能为空；扩大测试
范围时应明确添加人员，避免默认向全公司开放。

`DINGTALK_HANDLER_USERS` 是可以处理人工工单的 staffId，必须同时存在于
`DINGTALK_ALLOWED_USERS`。配置后，机器人会通过企业内部机器人主动单聊接口向
这些人员发送待处理通知。启动脚本会读取钉钉控制台中的真实 robotCode；直接通过
环境变量启动时必须配置 `DINGTALK_ROBOT_CODE`。应用还需要具备企业内部机器人
单聊消息发送接口对应的权限。

`DINGTALK_OPERATOR_USERS` 是跨业务板块的项目管理员 staffId，必须同时存在于
`DINGTALK_HANDLER_USERS`。项目管理员会加入后续新建协作群，并可执行与对应业务
负责人相同的生成、修改和确认结案操作。

`DINGTALK_DOMAIN_HANDLERS` 用 `业务板块=staffId` 指定每个部门的唯一负责人，
多个板块用逗号分隔，例如 `shareholder=staff-1,finance=staff-2`。被指定的人员
必须同时存在于 `DINGTALK_HANDLER_USERS`；已配置的板块只通知对应负责人，未配置
板块暂时回退给全部处理人，便于灰度期间避免工单遗漏。

## V0.7 三人协作群闭环

`DINGTALK_AUTO_GROUP_ENABLED=true` 后，员工在单聊中提出无法可靠回答的业务问题，
系统按 `DINGTALK_DOMAIN_HANDLERS` 找到对应负责人，通过当前已登录的 DWS 身份创建
内部协作群、加入企业机器人并发送问题摘要。创建成功后，员工和负责人直接在群内
沟通；原私聊工单通知只作为建群失败时的兜底。

本机灰度可以在启动命令中追加：

```powershell
-EnableAutoGroup `
-DwsProfile "<corp-id>:<user-id>"
```

自动建群依赖 DWS 用户登录态。当前登录用户会成为群创建者；正式扩大到其他员工前，
必须改用公司服务账号或具备代建群能力的正式应用身份，避免开发人员长期成为所有
协作群成员。普通机器人也不保证实时收到所有未 `@` 的群消息，因此知识沉淀还需要
在结案时通过有权账号补拉完整群记录。

## V0.6 钉钉人工处理闭环

出现知识不足、系统不可用、员工回复“未解决”或“转人工”时，系统生成工单并
主动私聊配置的处理人。收到最新一张工单后，处理人可以直接私聊机器人：

```text
我来处理
查看详情
答复：这里填写已核实的正确答案
```

直接答复时系统会自动完成领取，答案发送给原提问员工后才关闭工单。如果当前
有多张工单且无法安全判断上下文，机器人会列出待选工单，此时使用完整指令：

```text
查看 CS-20260727-000006
领取 CS-20260727-000006
答复 CS-20260727-000006 这里填写已核实的正确答案
```

只有配置的处理人能够使用工单命令。同一工单只能由一人领取；机器人把人工答案
主动私聊给原提问员工，发送成功后才关闭工单。负责人随后直接选择：

```text
设为标准答案
仅本次答复
```

前者把同一部门负责人同时记录为审核人，并进入自动检查与知识库发布流程；后者
标记为不可复用，不进入公共知识库。敏感个人信息和高影响财务事项不能由该快捷
动作直接发布，必须升级确认。若同时有多份答案待判断，可在命令后附工单编号。

协作群已使用钉钉 StandardCard 提供“完成并沉淀”“继续沟通”按钮；
自然语言短指令继续作为回调异常时的兜底。钉钉内看板后续继续调用同一套状态与权限
接口，不改变工单数据模型。

`OPENCLAW_SHAREHOLDER_ENABLED` 默认是 `false`。只有公司 Mac 已安装
`shareholder-3-0` 独立 Agent，且 Dify 可从该主机访问时才改为 `true`。
OpenClaw 不可用、输出无法解析或新增证据中不存在的数字时，系统自动返回原有
Dify 答案；Dify 未命中已审核股东文档时则直接转负责人，不调用 OpenClaw。

`DINGTALK_STORE_OPERATOR_ID` 不是 staffId，而是对门店宽表有访问权的钉钉用户
unionId。它只用于让 AI 表格按该用户已有的表格权限执行只读查询，不代表机器人
可以修改该用户或表格数据。

## 门店信息宽表

本机存在 `config/store-directory.local.json` 时，Stream worker 会启用门店
实时查询；文件不存在或 `DINGTALK_STORE_CONFIG_PATH` 为空时自动关闭。
配置模板见 `config/store-directory.example.json`。

`DINGTALK_STORE_PROVIDER=dws` 用于本机桥接调试，依赖当前电脑上的个人钉钉
登录态。生产环境必须使用 `openapi`：机器人通过自己的 AppKey/AppSecret 获取
应用 access token，再使用固定操作者 unionId 调用 AI 表格只读 OpenAPI。

OpenAPI 提供器不会读取整行。每次请求只包含门店名称字段和当前问题所需的
白名单字段，并在本地做门店名称精确匹配；手机号、基本户账号、法人登记证号、
租金及未经批准的人员关系不会进入服务。access token 只缓存在进程内，AppSecret
不会写入门店配置文件或日志。

正式启用前，机器人应用必须具备 `Notable.Base.Read.All`（AI 表格应用读权限），
并确保 `DINGTALK_STORE_OPERATOR_ID` 对应用户可以打开该门店宽表。不申请
`Notable.Base.Write.All` 或其他表格写权限。

## 真实问题闭环

Stream 适配器把每次业务提问、机器人原始回答、业务板块、处理动作和原因写入
`DINGTALK_STATE_DB_PATH` 对应的 SQLite 数据库。问候和告别等非业务会话会保留
审计记录但直接关闭，不进入业务反馈指标。

问题在写入台账前会脱敏手机号、邮箱、身份证号和银行卡号；机器人业务判断仍使用
本次消息原文，脱敏仅作用于后续审计和CSV导出，避免记录功能造成二次泄露。

正常业务回答末尾会邀请用户回复：

- `已解决`：上一条待评价回答更新为 `resolved`；
- `未解决`：上一条回答更新为 `needs_human`，生成问题编号；
- `转人工`：主动请求人工，上一条回答更新为 `needs_human`；
- Dify 返回知识不足或安全策略要求人工核实时，无需用户再次反馈，问题直接进入
  `needs_human`。
- Dify或其他回答后端不可用时，问题以 `system_unavailable` 原因进入
  `needs_human`，避免系统故障导致真实问题丢失。

反馈只会更新同一员工、同一钉钉会话中的最近一条待评价记录，不会串到其他员工
或其他群聊。机器人重复收到同一钉钉消息时仍由原有消息幂等逻辑拦截。

导出全部真实问题：

```powershell
.\.venv\Scripts\python.exe -m app.interaction_export `
  --database .runtime\customer-service\dingtalk.db `
  --output artifacts\dingtalk-interactions.csv
```

仅导出待人工处理问题：

```powershell
.\.venv\Scripts\python.exe -m app.interaction_export `
  --database .runtime\customer-service\dingtalk.db `
  --status needs_human `
  --output artifacts\dingtalk-needs-human.csv
```

CSV 使用 UTF-8 BOM，并对公式前缀进行转义，可直接用 Excel 打开。表内预留负责人、
最终权威答案、审核人和知识审核状态字段。

## 待人工问题处理

运营管理接口与钉钉Stream必须指向同一个数据库：

```powershell
$env:DINGTALK_STATE_DB_PATH = ".runtime\customer-service\dingtalk.db"
```

配置`OPERATIONS_API_KEY`并启动Web服务后，可在`/operations`页面处理钉钉
待人工问题。页面和接口不会返回钉钉消息ID、员工ID或会话ID。

处理流程：

1. 查看员工原始问题、机器人原始回答、问题编号和转人工原因；
2. 填写负责人并领取问题；
3. 负责人通过钉钉向员工回复最终权威答案；
4. 在运营页面填写同一答案和审核人；
5. 选择`可沉淀知识`或`仅个案处理`；
6. 点击`确认已回复并完成`。

受保护接口：

```text
GET   /v1/operations/dingtalk-interactions?status=needs_human
PATCH /v1/operations/dingtalk-interactions/{ticket_code}/claim
PATCH /v1/operations/dingtalk-interactions/{ticket_code}/resolve
```

接口使用`X-Operations-Key`请求头鉴权。同一问题只能由一个负责人领取，只有领取人
能够完成处理。

知识状态含义：

- `approved`：答案已审核，可以作为后续知识库候选；
- `not_reusable`：答案只适用于当前个案，不进入通用知识候选；
- `not_reviewed`：尚未完成知识审核。

`approved`不会自动把内容发布到Dify。知识发布仍需经过资料整理、负责人审核和
问答验收。

## 三人协作群闭环

设置 `DINGTALK_AUTO_GROUP_ENABLED=true` 后，知识不足的私聊问题会优先创建
“员工 + 业务负责人 + 机器人”内部协作群。建群失败时保留原私聊工单通知作为
兜底。每张新问题工单建立独立协作群；只有同一工单因重试再次触发时，才幂等
复用该工单已经建立的群，避免不同问题上下文混在一起。

自动建群依赖 `DINGTALK_DOMAIN_HANDLERS` 为业务板块配置唯一负责人，并通过
DWS 用户态创建群、添加企业机器人和补拉群历史。未完成真实灰度前保持开关为
`false`；当前本机股东 3.0 灰度已临时开启，扩大范围前仍需完成真实结案验收。

新建协作群后，机器人会自动发送结案操作卡片。业务负责人或项目管理员可以点击：

- `完成并沉淀`：补拉完整群历史，只把负责人或项目管理员的明确发言作为业务结论，
  完成脱敏与完整性检查后直接写入 Dify 正式知识库并结案；
- `继续沟通`：保持工单为待处理状态，不生成结案内容或发布知识。

文字指令继续作为兜底：

- `总结并结案`：补拉该群从问题创建以来的消息，脱敏并去重保存，生成结案草稿；
- `修改结案：核实后的完整答案`：替换草稿中的最终答复；
- `确认结案`：保存最终答复和问题成因，并标记为可发布候选知识。
- `完成并沉淀`：执行与卡片主按钮相同的一键结案和正式入库流程。

正常情况下一个协作群只对应一张工单。历史异常数据或人工迁移导致同一群关联多张
工单时，才使用 `总结并结案 CS-XXXXXXXX-XXXXXX` 明确指定问题。
只有该业务板块负责人或 `DINGTALK_OPERATOR_USERS` 中的项目管理员可以操作卡片。
点击“完成并沉淀”本身即视为负责人业务确认，不再要求同一负责人二次审批。
若未识别到负责人明确回复、总结仍提示需要补充、Dify 写入失败，工单均保持待处理；
旧的草稿与确认指令暂时保留为回调异常时的兼容路径。

群历史通过 `dws chat message list --group ... --direction newer --format json`
分页拉取。历史拉取失败、返回空记录或 AI 不能生成结构化草稿时，问题保持
`needs_human`，不会被错误关闭。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest `
    tests.test_dingtalk_adapter `
  tests.test_dingtalk_collaboration `
  tests.test_dingtalk_collaboration_closure `
  tests.test_dingtalk_stream_agent `
  tests.test_interaction_export `
  tests.test_openclaw_brain
.\.venv\Scripts\python.exe -m pip check
```

真实验收至少覆盖：

1. 正常知识问答。
2. 同一员工连续追问。
3. Stream 重启后继续追问。
4. 敏感信息拦截。
5. 未知问题转负责人。
6. 不同员工和不同会话不共享上下文。
7. 门店实时查询只返回白名单字段。
8. 门店人员关系、账户和金额类问题仍被拦截。
9. 关闭个人 DWS 登录态后，`openapi` 模式仍能查询真实门店。
10. 撤销 AI 表格只读权限后查询安全失败，不回退到 Dify 猜测答案。
11. 正常业务回答能记录并接受“已解决/未解决”反馈。
12. 知识不足和主动转人工问题能进入 `needs_human` 导出清单。
13. 待人工问题只能由一个负责人领取。
14. 只有领取人能填写最终答案并完成审核。
15. 运营页面在桌面和手机视口均无横向溢出。
