# 钉钉机器人与 OpenClaw 的 Mac 部署方案

## 结论

部署分两次开关完成：先把现有钉钉机器人迁到公司 Mac 常驻运行，确认消息、自动建群、群聊补拉、结案沉淀均正常；再开启 OpenClaw 股东 3.0 Agent。任何时刻只运行一个钉钉 Stream 客户端。

OpenClaw 是已审核证据之上的理解与表达增强层。Dify 仍负责检索已审核知识，Python 仍负责权限、风险拦截、转人工和输出校验。OpenClaw 不直接绑定钉钉，也不直接发送消息。

## 后续版本一键升级

Mac 首次迁移完成后，后续业务功能更新不再逐段复制命令。下载并解压新的完整部署包，在部署包根目录只运行：

```bash
bash upgrade-macos.command
```

升级入口会自动从现有 LaunchAgent 识别项目目录，校验部署包清单，备份当前应用代码，暂停 Stream，覆盖应用文件，按需更新 Python 依赖，执行生产预检和关键回归测试，然后恢复原有常驻服务。它不会覆盖或删除以下运行数据：

- Mac 专用 `.env`；
- `.runtime` 中的 Dify、SQLite 数据库、日志和安全迁移文件；
- 现有 `.venv`；
- `enterprise.local.json` 与 `store-directory.local.json`。

如果服务在升级前处于停止状态，升级后也保持停止；确实需要同时启动时使用 `bash upgrade-macos.command --start`。首次安装若尚未导入加密运行配置，脚本只安装代码并明确停在配置导入环节，不会在缺少密钥的情况下启动机器人。

## 上线前置条件

- 当前真实工单完成一次“员工提问 → 自动建群 → 负责人回复 → 完成并沉淀 → Dify 可检索”的闭环。
- 公司 Mac 上的项目代码、Mac 专用 `.env`、SQLite 数据库和 Dify 网络地址已准备好。
- Mac 已安装 Python 3、OpenClaw 与 DWS；OpenClaw Gateway 正常运行。
- `.env` 只保存在 Mac 本地，权限建议为 `600`，不得通过聊天或代码仓库传递。
- 已明确 DWS 使用的钉钉身份。灰度期可以暂用获授权的个人身份，正式生产应迁移到应用/服务身份 OpenAPI，避免个人退出登录后自动建群和历史补拉失效。

当前 Windows 配置中的 `DIFY_BASE_URL=http://127.0.0.1:8081` 不能直接复制到 Mac：它在 Mac 上会指向 Mac 自己。正式方案应把 Dify 及其知识库迁到 Mac/公司服务器，或改用公司可长期访问的 Dify 地址。把地址临时改成 Windows 局域网 IP 只适合短期灰度，因为 Windows 关机后机器人仍会失去问答能力。

### 当前 Dify 盘点与推荐迁移方式

2026-07-29 的只读盘点结果：

- Dify 版本：`1.15.0`。
- 正式知识库当前有 2 份已启用且索引完成的文档：
  - `enterprise-knowledge-shareholder.md`
  - `shareholder-store-expansion-guidance.md`
- 两份文档的源文件均保存在工作区知识包中。
- Windows Docker 中 PostgreSQL 数据约 89MB，插件数据约 100MB；数据量不是迁移瓶颈。

基于当前数据规模，推荐在 Mac 上安装同版本 Dify，重新配置 DeepSeek，创建新知识库并从已审核源文件重新导入，再重新发布 Chatflow。这样不直接搬运 Windows 与 Mac 之间的 PostgreSQL、Weaviate 和插件运行数据，跨平台风险更低。旧 Windows Dify 在 Mac 验收完成前保持原样，作为回滚环境。

新 Dify 必须通过以下检查后才能切流量：

1. 两份文档均显示已启用、索引完成。
2. 新环境的知识库 ID、知识库 API Key、应用 API Key 已写入 Mac 本地 `.env`。
3. 股东 3.0 自动测试集通过。
4. 钟睿恒使用真实口语化问题完成一次问答与一次转人工。
5. 自动建群、历史补拉、完成并沉淀能够写入新知识库。
6. OpenClaw 关闭时纯 Dify 链路正常，开启后回答仍受证据和安全校验约束。

## 第零阶段：Mac 主机体检与 Dify 预安装

这一阶段不会启动钉钉 Stream，也不会影响 Windows 上当前运行的机器人。

```bash
cd /Users/smallworks/services/ai-customer-service
chmod +x scripts/macos/*.sh
scripts/macos/host-preflight.sh
```

主机体检会只读检查 macOS 架构、磁盘、Docker/Colima、Python、DWS、OpenClaw，以及 `8081`、`8443`、`18789`、`18791` 等关键端口，不会安装或停止任何服务。

确认 Docker 守护进程可用后，安装并校验固定版本 Dify：

```bash
scripts/macos/setup-dify.sh
scripts/macos/run-dify.sh start
scripts/macos/run-dify.sh status
```

`setup-dify.sh` 默认固定为 `1.15.0`，只生成配置并校验 Docker Compose，不会自动启动。只有显式增加 `--start` 才会在安装结束后启动。Dify 仅绑定 `127.0.0.1:8081/8443`，不会直接暴露到局域网。

首次启动后，在 Mac 浏览器打开 `http://127.0.0.1:8081`，配置 DeepSeek、导入交付包 `knowledge-approved-sources` 目录中的两份已审核文档，并重新发布 Chatflow。新环境验收通过前，Windows Dify 保持原样作为回滚环境。

## 第一阶段：迁移现有机器人

1. 将项目复制到 Mac，例如 `/Users/smallworks/services/ai-customer-service`。
2. 创建轻量虚拟环境：

   ```bash
   cd /Users/smallworks/services/ai-customer-service
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-dingtalk.txt
   chmod +x scripts/macos/*.sh
   scripts/macos/preflight.sh
   ```

3. 以现有 `.env` 为基线生成 **Mac 专用配置**，保留 Dify 与钉钉密钥，但要重新填写 Mac 上的 Dify 地址、DWS 可执行文件绝对路径和 OpenClaw 可执行文件路径。不得直接照搬 Windows 的 `E:\...` 路径或 `127.0.0.1` 地址。
4. 复制 `.runtime/customer-service/dingtalk.db`。复制数据库前先停止 Windows Stream，防止两份状态分叉；复制后不要把旧端再次启动。
5. 先在 Mac 前台运行 `scripts/macos/run-dingtalk-stream.sh`，仅用钟睿恒账号完成冒烟测试。
6. 前台验证通过后停止进程，执行 `scripts/macos/install-launch-agent.sh`。脚本只生成 LaunchAgent，不自动抢占当前连接。
7. 确认 Windows Stream 已停止后，在 Mac 启动：

   ```bash
   launchctl bootstrap gui/$(id -u) \
     "$HOME/Library/LaunchAgents/com.shenhuo.dingtalk-internal-service.plist"
   ```

8. 查看状态和日志：

   ```bash
   launchctl print gui/$(id -u)/com.shenhuo.dingtalk-internal-service
   tail -f .runtime/customer-service/dingtalk-stream.stderr.log
   ```

## 第二阶段：安装并启用 OpenClaw Agent

不要使用路径异常的 v0.1 包。当前交付使用已校验为 POSIX 路径的 v0.4：

```bash
scripts/macos/install-openclaw-agent.sh \
  "$HOME/Desktop/shareholder-3-0-openclaw-package-v0.4.zip"

openclaw agent \
  --agent shareholder-3-0 \
  --message "申请入股需要走 OA 吗？" \
  --session-id shareholder-3-0-smoke \
  --thinking low \
  --timeout 90 \
  --json

.venv/bin/python scripts/run_openclaw_shareholder_acceptance.py \
  --agent shareholder-3-0 \
  --output .runtime/shareholder-3-0-acceptance.json
```

独立冒烟和验收通过后，在 `.env` 中设置：

```dotenv
OPENCLAW_SHAREHOLDER_ENABLED=true
OPENCLAW_EXECUTABLE=openclaw
OPENCLAW_SHAREHOLDER_AGENT_ID=shareholder-3-0
OPENCLAW_SHAREHOLDER_THINKING=low
OPENCLAW_SHAREHOLDER_TIMEOUT_SECONDS=90
```

随后只重启 Mac 上的 LaunchAgent：

```bash
launchctl kickstart -k gui/$(id -u)/com.shenhuo.dingtalk-internal-service
```

## 验收顺序

1. 已审核股东问题能正常回答，答案不新增数字或制度。
2. 模糊问法能被理解，但检索不到证据时仍转负责人。
3. 员工发送“未解决”后，每个独立问题创建独立协作群。
4. 协作群中未 `@机器人` 的人工对话可在结案时通过 DWS 历史补拉。
5. 负责人或刘子坚点击“完成并沉淀”后，只采纳有效业务结论，测试语和闲聊不入库。
6. 停止 OpenClaw 或制造超时，机器人应自动退回 Dify，不中断员工服务。
7. 重启 Mac 后 LaunchAgent 自动恢复，且没有第二个 Windows Stream 同时在线。

## 回滚

- 只回滚 OpenClaw：将 `OPENCLAW_SHAREHOLDER_ENABLED=false`，再 `launchctl kickstart -k`，即可恢复纯 Dify 链路。
- 暂停 Mac 机器人：

  ```bash
  launchctl bootout gui/$(id -u) \
    "$HOME/Library/LaunchAgents/com.shenhuo.dingtalk-internal-service.plist"
  ```

- 只有确认 Mac 已停止后，才允许重新启动 Windows Stream。
- Agent 升级脚本的 `--upgrade` 模式会先备份旧工作区，不改现有钉钉路由和 Agent 配置。
