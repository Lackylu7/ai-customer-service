#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PYTHON="$PROJECT_ROOT/.venv/bin/python"

[ -x "$PYTHON" ] || {
  printf 'Missing project virtualenv: %s\n' "$PYTHON" >&2
  exit 1
}

cd "$PROJECT_ROOT"
mkdir -p .runtime/customer-service
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

exec "$PYTHON" -m app.dingtalk_stream_agent
