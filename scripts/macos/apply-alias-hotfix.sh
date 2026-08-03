#!/bin/bash
set -eu

PROJECT="${1:-$HOME/services/ai-customer-service-deploy-v0.2/ai-customer-service-deploy}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_APP="$SCRIPT_DIR/app/dingtalk_collaboration.py"
SOURCE_TEST="$SCRIPT_DIR/tests/test_dingtalk_collaboration.py"
TARGET_APP="$PROJECT/app/dingtalk_collaboration.py"
TARGET_TEST="$PROJECT/tests/test_dingtalk_collaboration.py"
LABEL="com.shenhuo.dingtalk-internal-service"
LOG="$PROJECT/.runtime/customer-service/dingtalk-stream.stderr.log"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

[ "$(uname -s)" = "Darwin" ] || fail "this hotfix must run on macOS"
[ -f "$TARGET_APP" ] || fail "target project is missing: $PROJECT"
[ -f "$SOURCE_APP" ] || fail "hotfix application file is missing"
[ -f "$SOURCE_TEST" ] || fail "hotfix regression test is missing"

app_hash=$(shasum -a 256 "$SOURCE_APP" | awk '{print toupper($1)}')
test_hash=$(shasum -a 256 "$SOURCE_TEST" | awk '{print toupper($1)}')
[ "$app_hash" = "4A4FE22A8F3291F6612679E2CD02C4EBA9685765BC1E876B439788F150EC1A21" ] \
  || fail "application hotfix checksum mismatch"
[ "$test_hash" = "E46B26D9A08A981EEA2BEBEA330334CD5EB2F5595349D58E50E56417FC4CFD07" ] \
  || fail "test hotfix checksum mismatch"

stamp=$(date +%Y%m%d-%H%M%S)
backup_app="$TARGET_APP.pre-alias-hotfix-$stamp"
backup_test="$TARGET_TEST.pre-alias-hotfix-$stamp"
cp "$TARGET_APP" "$backup_app"
cp "$TARGET_TEST" "$backup_test"

temporary_app="$TARGET_APP.hotfix-$stamp.tmp"
temporary_test="$TARGET_TEST.hotfix-$stamp.tmp"
cp "$SOURCE_APP" "$temporary_app"
cp "$SOURCE_TEST" "$temporary_test"
mv "$temporary_app" "$TARGET_APP"
mv "$temporary_test" "$TARGET_TEST"

cd "$PROJECT"
if ! .venv/bin/python -m unittest \
  tests.test_dingtalk_collaboration \
  tests.test_dingtalk_collaboration_closure \
  tests.test_dingtalk_stream_agent; then
  cp "$backup_app" "$TARGET_APP"
  cp "$backup_test" "$TARGET_TEST"
  fail "hotfix tests failed; original files were restored"
fi

log_size=0
if [ -f "$LOG" ]; then
  log_size=$(wc -c < "$LOG" | tr -d ' ')
fi

launchctl kickstart -k "gui/$(id -u)/$LABEL"
sleep 5
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null |
  grep -q 'state = running' || fail "Mac Stream is not running after restart"

printf '=== 本次重启新增日志 ===\n'
if [ -f "$LOG" ]; then
  tail -c "+$((log_size + 1))" "$LOG"
fi

printf '\nSUCCESS: DWS 联系人别名热修复已应用，Mac Stream 正在运行。\n'
