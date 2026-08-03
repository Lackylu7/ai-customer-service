#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
RUNTIME="$PROJECT_ROOT/.runtime/customer-service"
PENDING="$RUNTIME/pending-upgrade.json"
PROCESSING="$RUNTIME/processing-upgrade.json"
STATUS_FILE="$RUNTIME/upgrade-status.txt"
LOCK="$RUNTIME/upgrade.lock"
STAGING=""

mkdir -p "$RUNTIME"
if ! mkdir "$LOCK" 2>/dev/null; then
  printf '已有升级任务正在运行。\n' > "$STATUS_FILE"
  exit 1
fi
cleanup() {
  if [ -n "$STAGING" ] && [ -d "$STAGING" ]; then
    rm -rf -- "$STAGING"
  fi
  rmdir "$LOCK" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

[ -f "$PENDING" ] || { printf '没有待执行的升级任务。\n' > "$STATUS_FILE"; exit 0; }
mv "$PENDING" "$PROCESSING"

eval "$(python3 - "$PROCESSING" "$HOME" <<'PY'
import json
import shlex
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
home = Path(sys.argv[2]).resolve()
package = Path(payload["package"]).resolve()
allowed = {home / "Downloads", home / "Desktop"}
if package.parent not in allowed:
    raise SystemExit("安装包不在允许目录")
print("PACKAGE=" + shlex.quote(str(package)))
print("VERSION=" + shlex.quote(str(payload["version"])))
print("EXPECTED_HASH=" + shlex.quote(str(payload["sha256"])))
PY
)"

if [ ! -f "$PACKAGE" ]; then
  printf '%s：失败，安装包不存在。\n' "$VERSION" > "$STATUS_FILE"
  exit 1
fi
ACTUAL_HASH=$(shasum -a 256 "$PACKAGE" | awk '{print toupper($1)}')
if [ "$ACTUAL_HASH" != "$EXPECTED_HASH" ]; then
  printf '%s：失败，安装包哈希发生变化。\n' "$VERSION" > "$STATUS_FILE"
  exit 1
fi

STAGING=$(mktemp -d "${TMPDIR:-/tmp}/shenhuo-upgrade.XXXXXX")
ditto -x -k "$PACKAGE" "$STAGING"
SOURCE="$STAGING/ai-customer-service-deploy"
if [ ! -f "$SOURCE/upgrade-and-enable-openclaw.command" ]; then
  printf '%s：失败，安装包结构不完整。\n' "$VERSION" > "$STATUS_FILE"
  exit 1
fi

if bash "$SOURCE/upgrade-and-enable-openclaw.command"; then
  printf '%s：升级成功，服务已重启并通过检查。\n' "$VERSION" > "$STATUS_FILE"
  rm -f -- "$PROCESSING"
else
  printf '%s：升级失败，安装器已尝试自动回滚。\n' "$VERSION" > "$STATUS_FILE"
  exit 1
fi
