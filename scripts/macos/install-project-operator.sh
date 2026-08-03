#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
LABEL="com.shenhuo.dingtalk-updater"
DOMAIN="gui/$(id -u)/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_ROOT/.runtime/customer-service"
python3 - "$PLIST" "$PROJECT_ROOT" "$HOME" <<'PY'
import plistlib
import sys
from pathlib import Path

plist = Path(sys.argv[1])
project = Path(sys.argv[2]).resolve()
home = Path(sys.argv[3]).resolve()
payload = {
    "Label": "com.shenhuo.dingtalk-updater",
    "ProgramArguments": [
        "/bin/bash",
        str(project / "scripts/macos/run-pending-upgrade.sh"),
    ],
    "WorkingDirectory": str(project),
    "EnvironmentVariables": {
        "PATH": (
            f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
            "/usr/bin:/bin:/usr/sbin:/sbin"
        ),
    },
    "StandardOutPath": str(
        project / ".runtime/customer-service/project-updater.stdout.log"
    ),
    "StandardErrorPath": str(
        project / ".runtime/customer-service/project-updater.stderr.log"
    ),
    "ProcessType": "Background",
    "ThrottleInterval": 10,
}
plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
PY
chmod 600 "$PLIST"
plutil -lint "$PLIST" >/dev/null

if launchctl print "$DOMAIN" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN"
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST"
printf '受控升级服务已安装：%s\n' "$PLIST"
