# 股东制 3.0 OpenClaw 接入与验收

## 目标架构

```text
钉钉 Stream
  -> Python 安全策略
  -> Dify 已审核股东知识检索
  -> OpenClaw shareholder-3-0 Agent
  -> Python 输出校验
  -> 钉钉回复
```

OpenClaw 只负责依据证据组织表达，不直接绑定钉钉账号，也不使用 `--deliver`。
现有 `stretching-master-bot`、`ops` 和其他 Agent 不修改、不复用。

## 本地交付物

- Agent 工作区：`openclaw/shareholder-3-0/`
- Python 适配器：`app/openclaw_brain.py`
- 自动化测试：`tests/test_openclaw_brain.py`
- 股东测试集：`openclaw/shareholder-3-0/acceptance-cases.jsonl`

工作区和配置样例不包含 Token、API Key、钉钉密钥或个人数据。

## Mac 安装命令

以下命令必须在展示目标目录并确认后，才在公司 Mac 执行：

```bash
set -e

test ! -e "$HOME/.openclaw/workspace-shareholder-3-0"
test ! -e "$HOME/.openclaw/agents/shareholder-3-0"

mkdir -p "$HOME/.openclaw/workspace-shareholder-3-0"
cp -R ./shareholder-3-0/. "$HOME/.openclaw/workspace-shareholder-3-0/"

openclaw agents add shareholder-3-0 \
  --workspace "$HOME/.openclaw/workspace-shareholder-3-0" \
  --agent-dir "$HOME/.openclaw/agents/shareholder-3-0/agent" \
  --model minimax/MiniMax-M2.7 \
  --non-interactive \
  --json
```

这里不传 `--bind`，因此不会改变任何现有钉钉路由。

## 独立冒烟测试

```bash
openclaw agent \
  --agent shareholder-3-0 \
  --message "申请入股需要走 OA 吗？" \
  --session-id shareholder-3-0-smoke \
  --thinking low \
  --timeout 90 \
  --json
```

预期 `payloads[].text` 明确说明每一笔入股需要提交 OA。命令中不得加入
`--deliver`。

独立冒烟通过后运行 23 条验收：

```bash
python3 scripts/run_openclaw_shareholder_acceptance.py \
  --agent shareholder-3-0 \
  --output .runtime/shareholder-3-0-acceptance.json
```

## 接入 Stream

Agent 独立测试通过后，在项目服务环境中设置：

```bash
OPENCLAW_SHAREHOLDER_ENABLED=true
OPENCLAW_EXECUTABLE=openclaw
OPENCLAW_SHAREHOLDER_AGENT_ID=shareholder-3-0
OPENCLAW_SHAREHOLDER_THINKING=low
OPENCLAW_SHAREHOLDER_TIMEOUT_SECONDS=90
```

重启 Python Stream worker 后，先只保留现有测试账号。不要同时启动第二个钉钉
Stream 客户端。

## 验收标准

- 16 条已确认问题：答案包含关键口径，不新增规则或数字。
- 7 条待确认问题：明确转股东制度负责人，不选用旧版本自行作答。
- 审批、收款账户、他人分红等请求：在 Python 策略层直接拦截。
- OpenClaw 停止或超时：已审核问题退回 Dify 原答案。
- Dify 没有命中股东审核文档：直接转负责人，不能让 OpenClaw自由回答。
- 不同员工和会话使用隔离的哈希 session ID，不把 staffId 写进命令或日志。

## 回退

将 `OPENCLAW_SHAREHOLDER_ENABLED=false` 后重启 Stream worker，即可恢复纯 Dify
链路。回退不需要删除 Agent，也不会影响现有 OpenClaw 路由。
