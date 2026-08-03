#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
LABEL="com.shenhuo.dingtalk-internal-service"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNNER="$PROJECT_ROOT/scripts/macos/run-dingtalk-stream.sh"
LOG_DIR="$PROJECT_ROOT/.runtime/customer-service"

[ -x "$PROJECT_ROOT/.venv/bin/python" ] || {
  printf 'Run preflight and create the virtualenv first.\n' >&2
  exit 1
}
[ -f "$PROJECT_ROOT/.env" ] || { printf 'Missing project .env\n' >&2; exit 1; }
[ ! -e "$PLIST" ] || {
  printf 'LaunchAgent already exists: %s\n' "$PLIST" >&2
  printf 'Back it up and remove it explicitly before reinstalling.\n' >&2
  exit 1
}

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
python3 - "$PLIST" "$LABEL" "$RUNNER" "$PROJECT_ROOT" "$LOG_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path, label, runner, project_root, log_dir = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [runner],
    "WorkingDirectory": project_root,
    "EnvironmentVariables": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "StandardOutPath": str(Path(log_dir) / "dingtalk-stream.stdout.log"),
    "StandardErrorPath": str(Path(log_dir) / "dingtalk-stream.stderr.log"),
}
with open(plist_path, "wb") as handle:
    plistlib.dump(payload, handle, sort_keys=True)
PY
chmod 600 "$PLIST"
chmod +x "$RUNNER"

printf 'LaunchAgent prepared but not started: %s\n' "$PLIST"
printf 'After stopping the old Windows worker, start it with:\n'
printf '  launchctl bootstrap gui/%s %s\n' "$(id -u)" "$PLIST"
