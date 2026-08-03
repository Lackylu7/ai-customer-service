#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
RUNTIME="$PROJECT_ROOT/.runtime/customer-service"
LABEL="com.shenhuo.dingtalk-internal-service"
UPDATER_LABEL="com.shenhuo.dingtalk-updater"

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

python_runtime() {
  if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    printf '%s\n' "$PROJECT_ROOT/.venv/bin/python"
  else
    command -v python3
  fi
}

status() {
  if launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q 'state = running'; then
    SERVICE_STATUS="运行正常"
  else
    SERVICE_STATUS="未运行"
  fi
  if curl -fsS --max-time 3 http://127.0.0.1:8081 >/dev/null 2>&1; then
    DIFY_STATUS="可访问"
  else
    DIFY_STATUS="不可访问"
  fi
  printf '机器人服务：%s\nDify：%s\n' "$SERVICE_STATUS" "$DIFY_STATUS"
  if [ -f "$RUNTIME/upgrade-status.txt" ]; then
    printf '最近升级：%s\n' "$(cat "$RUNTIME/upgrade-status.txt")"
  else
    printf '最近升级：暂无自动升级记录\n'
  fi
  if lsof -nP -iTCP:18789 -sTCP:LISTEN >/dev/null 2>&1; then
    printf 'OpenClaw Gateway：运行正常\n'
  else
    printf 'OpenClaw Gateway：未监听\n'
  fi
}

summary() {
  PYTHON=$(python_runtime)
  "$PYTHON" - "$PROJECT_ROOT/.runtime/customer-service/dingtalk.db" <<'PY'
import sqlite3
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    print("项目数据：数据库尚未创建")
    raise SystemExit(0)

database = sqlite3.connect(path)
tables = {
    row[0] for row in database.execute(
        "select name from sqlite_master where type='table'"
    )
}
print("项目运营摘要")
if "dingtalk_interactions" in tables:
    total = database.execute("select count(*) from dingtalk_interactions").fetchone()[0]
    print(f"• 累计交互：{total}")
    for status, count in database.execute(
        "select status, count(*) from dingtalk_interactions group by status order by status"
    ):
        print(f"• {status}：{count}")
    active = database.execute(
        "select count(*) from dingtalk_interactions where collaboration_status='active'"
    ).fetchone()[0]
    recent = database.execute(
        "select count(*) from dingtalk_interactions where created_at >= ?",
        (int(time.time()) - 86400,),
    ).fetchone()[0]
    print(f"• 活跃协作群：{active}")
    print(f"• 最近24小时交互：{recent}")
for table, label in (
    ("dingtalk_collaboration_messages", "已采集协作消息"),
    ("dingtalk_collaboration_closure_drafts", "结案草稿"),
):
    if table in tables:
        count = database.execute(f"select count(*) from {table}").fetchone()[0]
        print(f"• {label}：{count}")
PY
}

recent_errors() {
  LOG="$RUNTIME/dingtalk-stream.stderr.log"
  [ -f "$LOG" ] || { printf '暂时没有运行日志。\n'; return; }
  PYTHON=$(python_runtime)
  "$PYTHON" - "$LOG" <<'PY'
import re
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
selected = [line for line in lines[-500:] if re.search(r"ERROR|WARNING|Traceback", line, re.I)]
if not selected:
    print("最近运行日志中没有 ERROR 或 WARNING。")
    raise SystemExit(0)
pattern = re.compile(r"(?i)(token|secret|authorization|api[_-]?key)(\s*[:=]\s*)\S+")
print("最近错误摘要：")
for line in selected[-12:]:
    print("• " + pattern.sub(r"\1\2***", line)[:500])
PY
}

knowledge_status() {
  PYTHON=$(python_runtime)
  "$PYTHON" - "$PROJECT_ROOT" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
values = {}
env_path = root / ".env"
if env_path.is_file():
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
print("知识库状态")
print("• Dify 地址：" + ("已配置" if values.get("DIFY_BASE_URL") else "未配置"))
print("• 数据集：" + ("已配置" if values.get("DIFY_DATASET_ID") else "未配置"))
print("• 知识库凭据：" + ("已配置" if values.get("DIFY_DATASET_API_KEY") else "未配置"))
resources = root / ".runtime/customer-service/dify-resources.json"
print("• Dify 资源清单：" + ("存在" if resources.is_file() else "缺失"))
approved = []
for directory in (root / "openclaw", root / "packages", root / "config", root / "docs"):
    if directory.is_dir():
        approved.extend(directory.glob("**/approved-*.md"))
        approved.extend(directory.glob("**/enterprise-knowledge-*.md"))
print(f"• 本地已审核知识文件：{len({p.resolve() for p in approved})}")
database_path = root / ".runtime/customer-service/dingtalk.db"
if database_path.is_file():
    database = sqlite3.connect(database_path)
    tables = {row[0] for row in database.execute("select name from sqlite_master where type='table'")}
    if "dingtalk_interactions" in tables:
        rows = database.execute(
            "select knowledge_status, count(*) from dingtalk_interactions group by knowledge_status"
        ).fetchall()
        for status, count in rows:
            print(f"• 候选知识 {status or '未标记'}：{count}")
PY
}

routes() {
  PYTHON=$(python_runtime)
  "$PYTHON" - "$PROJECT_ROOT/.env" "$PROJECT_ROOT/config/enterprise.local.json" <<'PY'
import json
import sys
from pathlib import Path

env = {}
env_path = Path(sys.argv[1])
if env_path.is_file():
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key.strip()] = value.strip()
print("业务路由状态")
handlers = env.get("DINGTALK_DOMAIN_HANDLERS", "")
print("• 负责人路由：" + (handlers if handlers else "未配置"))
operators = [item for item in env.get("DINGTALK_OPERATOR_USERS", "").split(",") if item.strip()]
print(f"• 项目操作员：{len(operators)} 人")
config_path = Path(sys.argv[2])
if config_path.is_file():
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("• 企业配置：无法解析")
    else:
        domains = payload.get("domains") if isinstance(payload, dict) else None
        print(f"• 企业业务域：{len(domains) if isinstance(domains, dict) else '已配置'}")
else:
    print("• 企业配置：使用环境变量/默认配置")
PY
}

run_tests() {
  PYTHON=$(python_runtime)
  cd "$PROJECT_ROOT"
  OUTPUT=$(mktemp "${TMPDIR:-/tmp}/shenhuo-tests.XXXXXX")
  if "$PYTHON" -m unittest \
    tests.test_openclaw_project_operator \
    tests.test_enterprise_assistant \
    tests.test_openclaw_brain \
    tests.test_dingtalk_stream_agent >"$OUTPUT" 2>&1; then
    tail -n 8 "$OUTPUT"
    rm -f -- "$OUTPUT"
  else
    tail -n 20 "$OUTPUT"
    rm -f -- "$OUTPUT"
    return 1
  fi
}

backup_project() {
  DESTINATION="$RUNTIME/operational-backups"
  mkdir -p "$DESTINATION"
  ARCHIVE="$DESTINATION/project-$(date +%Y%m%d-%H%M%S).tar.gz"
  cd "$PROJECT_ROOT"
  tar -czf "$ARCHIVE" app config docs openclaw scripts tests README.md requirements-dingtalk.txt
  chmod 600 "$ARCHIVE"
  printf '项目备份完成：%s\n' "$ARCHIVE"
}

help_text() {
  cat <<'EOF'
支持命令：status、summary、errors、test、knowledge-status、routes、backup、queue-upgrade <version>。
所有能力均通过固定子命令执行，不接受任意 Shell 文本。
EOF
}

queue_upgrade() {
  VERSION=${1:-}
  printf '%s' "$VERSION" | grep -Eq '^v[0-9]+\.[0-9]+(\.[0-9]+)?$' || \
    fail "版本格式不正确，请使用类似 v0.8 的格式。"
  PACKAGE_NAME="ai-customer-service-macos-deploy-$VERSION.zip"
  PACKAGE=""
  COUNT=0
  for directory in "$HOME/Downloads" "$HOME/Desktop"; do
    candidate="$directory/$PACKAGE_NAME"
    if [ -f "$candidate" ]; then
      PACKAGE="$candidate"
      COUNT=$((COUNT + 1))
    fi
  done
  [ "$COUNT" -gt 0 ] || fail "没有找到 $PACKAGE_NAME，请先下载到 Mac 的下载或桌面目录。"
  [ "$COUNT" -eq 1 ] || fail "下载和桌面目录中存在重复安装包，请只保留一份后重试。"

  mkdir -p "$RUNTIME"
  HASH=$(shasum -a 256 "$PACKAGE" | awk '{print toupper($1)}')
  python3 - "$PACKAGE" <<'PY'
import hashlib
import json
import sys
import zipfile
from pathlib import PurePosixPath

archive = sys.argv[1]
with zipfile.ZipFile(archive) as bundle:
    names = bundle.namelist()
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise SystemExit(f"安装包包含不安全路径: {name}")
    manifest_name = "ai-customer-service-deploy/deployment-manifest.json"
    if manifest_name not in names:
        raise SystemExit("安装包缺少文件清单")
    manifest = json.loads(bundle.read(manifest_name).decode("utf-8-sig"))
    for entry in manifest:
        relative = str(entry["path"])
        item = "ai-customer-service-deploy/" + relative
        if item not in names:
            raise SystemExit(f"安装包缺少文件: {relative}")
        digest = hashlib.sha256(bundle.read(item)).hexdigest().upper()
        if digest != str(entry["sha256"]).upper():
            raise SystemExit(f"安装包校验失败: {relative}")
print("安装包校验通过")
PY

  TEMPORARY="$RUNTIME/.pending-upgrade-$$.json"
  python3 - "$TEMPORARY" "$PACKAGE" "$VERSION" "$HASH" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

target = Path(sys.argv[1])
payload = {
    "package": str(Path(sys.argv[2]).resolve()),
    "version": sys.argv[3],
    "sha256": sys.argv[4],
    "queued_at": datetime.now(timezone.utc).isoformat(),
}
target.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
os.chmod(target, 0o600)
PY
  mv "$TEMPORARY" "$RUNTIME/pending-upgrade.json"
  nohup launchctl kickstart "gui/$(id -u)/$UPDATER_LABEL" \
    >> "$RUNTIME/project-updater.kickstart.log" 2>&1 </dev/null &
  printf '升级任务已受理：%s。机器人会自动校验、备份、测试、重启；稍后发送“龙虾：检查机器人状态”可查看结果。\n' "$VERSION"
}

case "${1:-}" in
  help)
    help_text
    ;;
  status)
    status
    ;;
  summary)
    status
    summary
    ;;
  errors)
    recent_errors
    ;;
  test)
    run_tests
    ;;
  knowledge-status)
    knowledge_status
    ;;
  routes)
    routes
    ;;
  backup)
    backup_project
    ;;
  queue-upgrade)
    queue_upgrade "${2:-}"
    ;;
  *)
    fail "不支持该项目管家子命令。运行 help 查看允许的能力。"
    ;;
esac
