#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
SOURCE="$PROJECT_ROOT/openclaw/project-manager-skill"
TARGET="$HOME/.openclaw/workspace/skills/shenhuo-internal-service-manager"

[ -f "$SOURCE/SKILL.md" ] || {
  printf 'Project manager skill is missing: %s\n' "$SOURCE/SKILL.md" >&2
  exit 1
}

if [ -d "$TARGET" ]; then
  BACKUP="$HOME/.openclaw/backups/shenhuo-project-manager-skill-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$(dirname -- "$BACKUP")"
  cp -R "$TARGET" "$BACKUP"
fi

mkdir -p "$TARGET"
cp "$SOURCE/SKILL.md" "$TARGET/SKILL.md"
chmod 600 "$TARGET/SKILL.md"
printf 'OpenClaw project manager skill installed: %s\n' "$TARGET"
