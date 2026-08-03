#!/bin/bash
set -eu

usage() {
  printf 'Usage: %s /absolute/path/shareholder-3-0-openclaw-package-v0.4.zip [--upgrade]\n' "$0" >&2
  exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
PACKAGE=$1
MODE=${2:-}
[ -f "$PACKAGE" ] || { printf 'Package not found: %s\n' "$PACKAGE" >&2; exit 1; }
[ -z "$MODE" ] || [ "$MODE" = "--upgrade" ] || usage
command -v python3 >/dev/null 2>&1 || { printf 'python3 is required\n' >&2; exit 1; }
command -v openclaw >/dev/null 2>&1 || { printf 'openclaw is required\n' >&2; exit 1; }

WORKSPACE="$HOME/.openclaw/workspace-shareholder-3-0"
AGENT_ROOT="$HOME/.openclaw/agents/shareholder-3-0"
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/shareholder-3-0.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT HUP INT TERM

python3 - "$PACKAGE" "$STAGING" <<'PY'
import sys
import zipfile
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
required = {
    "shareholder-3-0/AGENTS.md",
    "shareholder-3-0/IDENTITY.md",
    "shareholder-3-0/SOUL.md",
    "shareholder-3-0/USER.md",
    "shareholder-3-0/acceptance-cases.jsonl",
    "shareholder-3-0/knowledge/approved-shareholder-3-0.md",
}
with zipfile.ZipFile(archive) as bundle:
    names = set(bundle.namelist())
    for name in names:
        path = PurePosixPath(name)
        if "\\" in name or path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe or non-POSIX package entry: {name!r}")
        if path.parts and path.parts[0] != "shareholder-3-0":
            raise SystemExit(f"unexpected package root: {name!r}")
    missing = sorted(required - names)
    if missing:
        raise SystemExit("package is incomplete: " + ", ".join(missing))
    bundle.extractall(destination)
PY

SOURCE="$STAGING/shareholder-3-0"
if [ "$MODE" = "--upgrade" ]; then
  [ -d "$WORKSPACE" ] || { printf 'Existing workspace not found: %s\n' "$WORKSPACE" >&2; exit 1; }
  [ -d "$AGENT_ROOT" ] || { printf 'Existing agent not found: %s\n' "$AGENT_ROOT" >&2; exit 1; }
  BACKUP="$HOME/.openclaw/backups/workspace-shareholder-3-0-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$(dirname -- "$BACKUP")"
  cp -R "$WORKSPACE" "$BACKUP"
  find "$WORKSPACE" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  cp -R "$SOURCE/." "$WORKSPACE/"
  printf 'Workspace upgraded. Backup: %s\n' "$BACKUP"
else
  [ ! -e "$WORKSPACE" ] || { printf 'Workspace already exists; use --upgrade after review\n' >&2; exit 1; }
  [ ! -e "$AGENT_ROOT" ] || { printf 'Agent already exists; use --upgrade after review\n' >&2; exit 1; }
  mkdir -p "$WORKSPACE"
  cp -R "$SOURCE/." "$WORKSPACE/"
  openclaw agents add shareholder-3-0 \
    --workspace "$WORKSPACE" \
    --agent-dir "$AGENT_ROOT/agent" \
    --model minimax/MiniMax-M2.7 \
    --non-interactive \
    --json
  printf 'Agent installed without DingTalk binding or message delivery.\n'
fi

printf 'Workspace: %s\n' "$WORKSPACE"
