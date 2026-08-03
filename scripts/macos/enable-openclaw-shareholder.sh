#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_PATH="$PROJECT_ROOT/.env"
PACKAGE="$PROJECT_ROOT/packages/shareholder-3-0-openclaw-package-v0.4.zip"
LABEL="com.shenhuo.dingtalk-internal-service"
DOMAIN="gui/$(id -u)/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WORKSPACE="$HOME/.openclaw/workspace-shareholder-3-0"
AGENT_ROOT="$HOME/.openclaw/agents/shareholder-3-0"
BACKUP=""
CONFIG_CHANGED=0

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

rollback_on_error() {
  status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ] && [ "$CONFIG_CHANGED" -eq 1 ] && [ -f "$BACKUP" ]; then
    cp "$BACKUP" "$ENV_PATH"
    chmod 600 "$ENV_PATH"
    if launchctl print "$DOMAIN" >/dev/null 2>&1; then
      launchctl kickstart -k "$DOMAIN" >/dev/null 2>&1 || true
    fi
    printf 'OpenClaw configuration was restored after the failed activation.\n' >&2
  fi
  exit "$status"
}
trap rollback_on_error EXIT INT TERM

[ "$(uname -s)" = "Darwin" ] || fail "this script must run on macOS"
[ -f "$ENV_PATH" ] || fail "runtime .env is missing: $ENV_PATH"
[ -f "$PACKAGE" ] || fail "OpenClaw package is missing: $PACKAGE"
command -v openclaw >/dev/null 2>&1 || fail "openclaw is not installed"

printf '=== 1/6 Install or upgrade the isolated shareholder agent ===\n'
if [ -d "$WORKSPACE" ] && [ -d "$AGENT_ROOT" ]; then
  "$SCRIPT_DIR/install-openclaw-agent.sh" "$PACKAGE" --upgrade
elif [ ! -e "$WORKSPACE" ] && [ ! -e "$AGENT_ROOT" ]; then
  "$SCRIPT_DIR/install-openclaw-agent.sh" "$PACKAGE"
else
  fail "OpenClaw agent files are incomplete; review workspace and agent root"
fi
bash "$SCRIPT_DIR/install-openclaw-companion-agent.sh"

printf '=== 2/6 Install the OpenClaw project-manager skill ===\n'
bash "$SCRIPT_DIR/install-openclaw-project-manager-skill.sh"

printf '=== 3/6 Enable the evidence-first dialogue layer and project operator ===\n'
BACKUP_DIR="$PROJECT_ROOT/.runtime/customer-service/config-backups"
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/.env-before-openclaw-$(date +%Y%m%d-%H%M%S)"
cp "$ENV_PATH" "$BACKUP"
chmod 600 "$BACKUP"

python3 - "$ENV_PATH" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
updates = {
    "OPENCLAW_SHAREHOLDER_ENABLED": "true",
    "OPENCLAW_DIALOGUE_TIMEOUT_SECONDS": "30",
    "OPENCLAW_PROJECT_OPERATOR_ENABLED": "true",
    "OPENCLAW_GENERAL_CONVERSATION_ENABLED": "true",
}
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
result = []
for line in lines:
    key, separator, _ = line.partition("=")
    if separator and key in updates:
        result.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        result.append(line)
for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")

temporary = path.with_name(f".{path.name}.openclaw-{os.getpid()}.tmp")
temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
CONFIG_CHANGED=1

printf '=== 4/6 Install the isolated controlled-upgrade service ===\n'
bash "$SCRIPT_DIR/install-project-operator.sh"

printf '=== 5/6 Run regression checks ===\n'
cd "$PROJECT_ROOT"
.venv/bin/python -m unittest \
  tests.test_openclaw_brain \
  tests.test_openclaw_project_operator \
  tests.test_enterprise_assistant \
  tests.test_dingtalk_adapter \
  tests.test_macos_deployment_assets

printf '=== 6/6 Restart and verify the DingTalk service ===\n'
if launchctl print "$DOMAIN" >/dev/null 2>&1; then
  launchctl kickstart -k "$DOMAIN"
else
  [ -f "$PLIST" ] || fail "LaunchAgent plist is missing: $PLIST"
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
fi
sleep 5
launchctl print "$DOMAIN" | grep -E 'state =|pid =|last exit code' || true
launchctl print "$DOMAIN" | grep -q 'state = running' || \
  fail "DingTalk Stream did not remain running"

CONFIG_CHANGED=0
trap - EXIT INT TERM
printf '\nSUCCESS: OpenClaw dialogue layer is enabled and the Mac Stream is running.\n'
printf 'Configuration backup: %s\n' "$BACKUP"
