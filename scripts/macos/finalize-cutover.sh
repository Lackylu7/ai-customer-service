#!/bin/bash
set -eu

PROJECT="${1:-$HOME/services/ai-customer-service-deploy-v0.2/ai-customer-service-deploy}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATABASE="$SCRIPT_DIR/dingtalk-state-cutover-20260730.enc.json"
IMPORTER="$SCRIPT_DIR/import-state-database.js"
PRIVATE_KEY="$PROJECT/.runtime/secure-transfer/deepseek-transfer-private.pem"
LABEL="com.shenhuo.dingtalk-internal-service"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

[ "$(uname -s)" = "Darwin" ] || fail "this cutover package must run on macOS"
[ -d "$PROJECT" ] || fail "project directory does not exist: $PROJECT"
[ -f "$PROJECT/.env" ] || fail "Mac runtime configuration has not been imported"
[ -f "$PRIVATE_KEY" ] || fail "Mac transfer private key is missing"
[ -f "$DATABASE" ] || fail "encrypted state database is missing from the package"
[ -f "$IMPORTER" ] || fail "database importer is missing from the package"

database_hash=$(shasum -a 256 "$DATABASE" | awk '{print toupper($1)}')
importer_hash=$(shasum -a 256 "$IMPORTER" | awk '{print toupper($1)}')
[ "$database_hash" = "AAEDC3F335AB53A2937BAF99D5ED2E3DAFE8EA8D5CEF70BAD02FFB7CFCDAACE7" ] \
  || fail "encrypted state database checksum mismatch"
[ "$importer_hash" = "9256A6799BE06E29C7FA3A753A3508BA42255A02EA3309B709ABD8D957C35B7D" ] \
  || fail "database importer checksum mismatch"

if pgrep -f 'app[.]dingtalk_stream_agent' >/dev/null 2>&1; then
  fail "a DingTalk Stream process is already running on this Mac"
fi
[ ! -e "$PLIST" ] || fail "LaunchAgent already exists: $PLIST"

printf '=== 1/5 导入最终状态数据库 ===\n'
node "$IMPORTER" "$DATABASE" "$PRIVATE_KEY" "$PROJECT"

printf '=== 2/5 校验数据库完整性和迁移数量 ===\n'
cd "$PROJECT"
.venv/bin/python - <<'PY'
import sqlite3

database = sqlite3.connect(".runtime/customer-service/dingtalk.db")
if database.execute("pragma integrity_check").fetchone()[0] != "ok":
    raise SystemExit("database integrity check failed")
expected = {
    "dingtalk_interactions": 59,
    "dingtalk_processed_messages": 136,
    "dingtalk_collaboration_messages": 2,
}
for table, expected_count in expected.items():
    actual = database.execute(f"select count(*) from {table}").fetchone()[0]
    if actual != expected_count:
        raise SystemExit(
            f"unexpected row count for {table}: {actual}, expected {expected_count}"
        )
print("数据库完整性与迁移数量：OK")
database.close()
PY

printf '=== 3/5 运行启动前检查 ===\n'
scripts/macos/preflight.sh

printf '=== 4/5 安装 Mac 常驻服务 ===\n'
cp "$IMPORTER" "$PROJECT/scripts/macos/import-state-database.js"
chmod 700 "$PROJECT/scripts/macos/import-state-database.js"
scripts/macos/install-launch-agent.sh

printf '=== 5/5 启动并验证 Mac Stream ===\n'
launchctl bootstrap "gui/$(id -u)" "$PLIST"
started=0
attempt=0
while [ "$attempt" -lt 15 ]; do
  if launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null |
     grep -q 'state = running'; then
    started=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if [ "$started" -ne 1 ]; then
  printf 'Mac Stream did not remain running. Recent log output:\n' >&2
  tail -n 40 .runtime/customer-service/dingtalk-stream.stderr.log >&2 || true
  launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  fail "Mac Stream startup validation failed; service was stopped"
fi

printf '\nSUCCESS: Mac Stream is running as a persistent LaunchAgent.\n'
printf 'Windows Stream remains stopped.\n'
printf 'Log: %s\n' "$PROJECT/.runtime/customer-service/dingtalk-stream.stderr.log"
