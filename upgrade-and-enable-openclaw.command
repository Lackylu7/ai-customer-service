#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bash "$SCRIPT_DIR/scripts/macos/install-or-upgrade.sh" "$@"

LABEL="com.shenhuo.dingtalk-internal-service"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TARGET=""
if [ -f "$PLIST" ]; then
  TARGET=$(plutil -extract WorkingDirectory raw -o - "$PLIST" 2>/dev/null || true)
fi
if [ -z "$TARGET" ]; then
  TARGET="$HOME/services/ai-customer-service-deploy-v0.2/ai-customer-service-deploy"
fi
[ -f "$TARGET/scripts/macos/enable-openclaw-shareholder.sh" ] || {
  printf 'ERROR: deployed OpenClaw activation script is missing: %s\n' "$TARGET" >&2
  exit 1
}
bash "$TARGET/scripts/macos/enable-openclaw-shareholder.sh"
