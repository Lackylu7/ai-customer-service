#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
LABEL="com.shenhuo.dingtalk-internal-service"
DOMAIN="gui/$(id -u)/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LEGACY_TARGET="$HOME/services/ai-customer-service-deploy-v0.2/ai-customer-service-deploy"
TARGET=""
START_IF_STOPPED=0
SKIP_TESTS=0

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: bash upgrade-macos.command [--target PATH] [--start] [--skip-tests]

Safely installs or upgrades the DingTalk internal service on macOS.
Runtime data, .env, the virtualenv, Dify data, and local configuration are kept.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target)
      shift
      TARGET="${1:?missing value for --target}"
      ;;
    --start)
      START_IF_STOPPED=1
      ;;
    --skip-tests)
      SKIP_TESTS=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown option: $1"
      ;;
  esac
  shift
done

[ "$(uname -s)" = "Darwin" ] || fail "this installer must run on macOS"
for command_name in python3 rsync tar shasum launchctl plutil; do
  command -v "$command_name" >/dev/null 2>&1 || \
    fail "required command is missing: $command_name"
done
[ -f "$SOURCE_ROOT/requirements-dingtalk.txt" ] || \
  fail "deployment package is incomplete: requirements-dingtalk.txt is missing"
[ -f "$SOURCE_ROOT/app/dingtalk_stream_agent.py" ] || \
  fail "deployment package is incomplete: DingTalk Stream code is missing"

if [ -z "$TARGET" ] && [ -f "$PLIST" ]; then
  TARGET=$(plutil -extract WorkingDirectory raw -o - "$PLIST" 2>/dev/null || true)
fi
if [ -z "$TARGET" ] && [ -d "$LEGACY_TARGET" ]; then
  TARGET="$LEGACY_TARGET"
fi
if [ -z "$TARGET" ]; then
  TARGET="$HOME/services/ai-customer-service"
fi

case "$TARGET" in
  "$HOME"/services/*)
    ;;
  *)
    fail "target must stay under $HOME/services: $TARGET"
    ;;
esac
[ "$TARGET" != "$HOME/services" ] || fail "refusing to use the services root as target"
[ "$TARGET" != "$SOURCE_ROOT" ] || fail "source and target directories must be different"

MANIFEST="$SOURCE_ROOT/deployment-manifest.json"
if [ -f "$MANIFEST" ]; then
  printf '=== 1/7 Verify deployment package ===\n'
  python3 - "$MANIFEST" "$SOURCE_ROOT" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1]).resolve()
source_root = Path(sys.argv[2]).resolve()
entries = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
if not isinstance(entries, list) or not entries:
    raise SystemExit("deployment manifest is empty or invalid")

for entry in entries:
    relative = Path(entry["path"])
    if relative.is_absolute() or ".." in relative.parts or "\\" in entry["path"]:
        raise SystemExit(f"unsafe manifest path: {entry['path']}")
    candidate = (source_root / relative).resolve()
    if source_root not in candidate.parents:
        raise SystemExit(f"manifest path escapes source root: {entry['path']}")
    if not candidate.is_file():
        raise SystemExit(f"manifest file is missing: {entry['path']}")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest().upper()
    if digest != str(entry["sha256"]).upper():
        raise SystemExit(f"checksum mismatch: {entry['path']}")

print(f"Deployment manifest verified: {len(entries)} files")
PY
else
  printf '=== 1/7 Development tree detected; package manifest check skipped ===\n'
fi

TARGET_ALREADY_EXISTS=0
if [ -d "$TARGET" ]; then
  TARGET_ALREADY_EXISTS=1
fi
mkdir -p "$TARGET"

SERVICE_WAS_RUNNING=0
if launchctl print "$DOMAIN" >/dev/null 2>&1; then
  SERVICE_WAS_RUNNING=1
fi
BACKUP_PATH=""
CUTOVER_STARTED=0

rollback_on_error() {
  status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ] && [ "$CUTOVER_STARTED" -eq 1 ]; then
    printf '\nUpgrade failed; restoring the previous application code.\n' >&2
    if [ "$SERVICE_WAS_RUNNING" -eq 1 ]; then
      launchctl bootout "$DOMAIN" >/dev/null 2>&1 || true
    fi
    if [ -n "$BACKUP_PATH" ] && [ -f "$BACKUP_PATH" ]; then
      tar -xzf "$BACKUP_PATH" -C "$TARGET"
    fi
    if [ "$SERVICE_WAS_RUNNING" -eq 1 ] && [ -f "$PLIST" ]; then
      launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
    fi
  fi
  exit "$status"
}
trap rollback_on_error EXIT INT TERM

printf '=== 2/7 Back up the current application code ===\n'
if [ "$TARGET_ALREADY_EXISTS" -eq 1 ]; then
  BACKUP_DIR="$TARGET/.runtime/deployment-backups"
  mkdir -p "$BACKUP_DIR"
  BACKUP_PATH="$BACKUP_DIR/application-$(date +%Y%m%d-%H%M%S).tar.gz"
  backup_items=""
  for item in app config docs infra openclaw scripts tests README.md requirements-dingtalk.txt; do
    if [ -e "$TARGET/$item" ]; then
      backup_items="$backup_items $item"
    fi
  done
  if [ -n "$backup_items" ]; then
    # shellcheck disable=SC2086
    tar -czf "$BACKUP_PATH" -C "$TARGET" $backup_items
    chmod 600 "$BACKUP_PATH"
    printf 'Application backup: %s\n' "$BACKUP_PATH"
  else
    printf 'No previous application files required backup.\n'
  fi
else
  printf 'Fresh target; no previous application files required backup.\n'
fi

if [ "$TARGET_ALREADY_EXISTS" -eq 1 ]; then
  CUTOVER_STARTED=1
fi
if [ "$SERVICE_WAS_RUNNING" -eq 1 ]; then
  printf '=== 3/7 Stop the running DingTalk Stream ===\n'
  launchctl bootout "$DOMAIN"
else
  printf '=== 3/7 DingTalk Stream is already stopped ===\n'
fi

printf '=== 4/7 Install application files and dependencies ===\n'
rsync -a \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude '.runtime/' \
  --exclude '.venv/' \
  --exclude 'config/enterprise.local.json' \
  --exclude 'config/store-directory.local.json' \
  "$SOURCE_ROOT/" "$TARGET/"

chmod +x "$TARGET/scripts/macos/"*.sh
if [ ! -x "$TARGET/.venv/bin/python" ]; then
  python3 -m venv "$TARGET/.venv"
fi
if ! "$TARGET/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  printf 'The existing virtual environment has no pip; restoring it with ensurepip.\n'
  "$TARGET/.venv/bin/python" -m ensurepip --upgrade
fi

REQUIREMENTS_HASH=$(shasum -a 256 "$TARGET/requirements-dingtalk.txt" | awk '{print toupper($1)}')
REQUIREMENTS_MARKER="$TARGET/.runtime/customer-service/requirements-dingtalk.sha256"
mkdir -p "$(dirname -- "$REQUIREMENTS_MARKER")"
INSTALLED_HASH=""
if [ -f "$REQUIREMENTS_MARKER" ]; then
  INSTALLED_HASH=$(cat "$REQUIREMENTS_MARKER")
fi
if [ "$REQUIREMENTS_HASH" != "$INSTALLED_HASH" ]; then
  "$TARGET/.venv/bin/python" -m pip install \
    --disable-pip-version-check \
    -r "$TARGET/requirements-dingtalk.txt"
  printf '%s\n' "$REQUIREMENTS_HASH" > "$REQUIREMENTS_MARKER"
fi

if [ ! -f "$TARGET/.env" ]; then
  printf '\nApplication files are installed at %s.\n' "$TARGET"
  printf 'The service was not started because the Mac runtime .env has not been imported.\n'
  printf 'Import the encrypted runtime configuration, then rerun this installer with --start.\n'
  exit 3
fi

printf '=== 5/7 Run production preflight and regression tests ===\n'
cd "$TARGET"
scripts/macos/preflight.sh
if [ "$SKIP_TESTS" -eq 0 ]; then
  .venv/bin/python -m unittest \
    tests.test_macos_deployment_assets \
    tests.test_dingtalk_collaboration
else
  printf 'Regression tests skipped by operator request.\n'
fi

printf '=== 6/7 Prepare or restore the persistent service ===\n'
if [ ! -f "$PLIST" ]; then
  scripts/macos/install-launch-agent.sh
fi
if [ "$SERVICE_WAS_RUNNING" -eq 1 ] || [ "$START_IF_STOPPED" -eq 1 ]; then
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
else
  printf 'Service was stopped before the upgrade and remains stopped. Use --start to start it.\n'
fi

printf '=== 7/7 Verify runtime status ===\n'
if [ "$SERVICE_WAS_RUNNING" -eq 1 ] || [ "$START_IF_STOPPED" -eq 1 ]; then
  attempt=0
  while [ "$attempt" -lt 15 ]; do
    if launchctl print "$DOMAIN" 2>/dev/null | grep -q 'state = running'; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  launchctl print "$DOMAIN" 2>/dev/null | \
    grep -E 'state =|pid =|last exit code' || true
  if ! launchctl print "$DOMAIN" 2>/dev/null | grep -q 'state = running'; then
    tail -n 40 "$TARGET/.runtime/customer-service/dingtalk-stream.stderr.log" >&2 || true
    fail "DingTalk Stream did not remain running"
  fi
fi

printf '\nSUCCESS: Mac application installation/upgrade completed.\n'
printf 'Target: %s\n' "$TARGET"
printf 'Runtime configuration, Dify data, SQLite state, and local configuration were preserved.\n'
trap - EXIT INT TERM
