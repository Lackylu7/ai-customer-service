#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
SOURCE="$PROJECT_ROOT/openclaw/internal-companion"
WORKSPACE="$HOME/.openclaw/workspace-internal-companion"
AGENT_ROOT="$HOME/.openclaw/agents/internal-companion"

for name in AGENTS.md IDENTITY.md SOUL.md USER.md; do
  [ -f "$SOURCE/$name" ] || {
    printf 'Companion agent file is missing: %s\n' "$name" >&2
    exit 1
  }
done
command -v openclaw >/dev/null 2>&1 || {
  printf 'openclaw is required\n' >&2
  exit 1
}

if [ -d "$WORKSPACE" ] && [ -d "$AGENT_ROOT" ]; then
  BACKUP="$HOME/.openclaw/backups/workspace-internal-companion-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$(dirname -- "$BACKUP")"
  cp -R "$WORKSPACE" "$BACKUP"
  find "$WORKSPACE" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  cp -R "$SOURCE/." "$WORKSPACE/"
  printf 'Companion workspace upgraded. Backup: %s\n' "$BACKUP"
elif [ ! -e "$WORKSPACE" ] && [ ! -e "$AGENT_ROOT" ]; then
  mkdir -p "$WORKSPACE"
  cp -R "$SOURCE/." "$WORKSPACE/"
  openclaw agents add internal-companion \
    --workspace "$WORKSPACE" \
    --agent-dir "$AGENT_ROOT/agent" \
    --model minimax/MiniMax-M2.7 \
    --non-interactive \
    --json
  printf 'Companion agent installed without DingTalk binding or delivery.\n'
else
  printf 'Companion agent files are incomplete; review workspace and agent root\n' >&2
  exit 1
fi
