#!/bin/bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="$PROJECT_ROOT/.env"
PYTHON="$PROJECT_ROOT/.venv/bin/python"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

pass() {
  printf 'OK: %s\n' "$1"
}

[ "$(uname -s)" = "Darwin" ] || fail "this script must run on macOS"
pass "macOS detected"

[ -x "$PYTHON" ] || fail "missing project virtualenv: $PYTHON"
"$PYTHON" -c 'import dingtalk_stream' >/dev/null 2>&1 \
  || fail "dingtalk-stream is not installed in the project virtualenv"
pass "Python runtime and dingtalk-stream are ready"

[ -f "$ENV_FILE" ] || fail "missing .env file: $ENV_FILE"
[ ! -L "$ENV_FILE" ] || fail ".env must not be a symbolic link"

"$PYTHON" - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
values = {}
for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

required = (
    "DIFY_BASE_URL",
    "DIFY_APP_API_KEY",
    "DIFY_DATASET_ID",
    "DIFY_DATASET_API_KEY",
    "DINGTALK_CLIENT_ID",
    "DINGTALK_CLIENT_SECRET",
    "DINGTALK_ALLOWED_USERS",
    "DINGTALK_HANDLER_USERS",
    "DINGTALK_OPERATOR_USERS",
    "DINGTALK_DOMAIN_HANDLERS",
    "DINGTALK_ROBOT_CODE",
    "DINGTALK_STATE_DB_PATH",
)
missing = [key for key in required if not values.get(key)]
if missing:
    print("FAIL: missing required .env keys: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
print("OK: required .env keys are present (values were not printed)")
PY

"$PYTHON" - "$ENV_FILE" <<'PY'
import sys
import urllib.error
import urllib.request
from pathlib import Path

values = {}
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8-sig").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

base_url = values["DIFY_BASE_URL"].rstrip("/")
try:
    with urllib.request.urlopen(base_url, timeout=5) as response:
        response.read(1)
except urllib.error.HTTPError:
    # An HTTP response, including an authorization response, proves reachability.
    pass
except (urllib.error.URLError, TimeoutError, OSError) as exc:
    print(f"FAIL: Dify is not reachable from this Mac: {exc}", file=sys.stderr)
    raise SystemExit(1)
print("OK: Dify endpoint is reachable from this Mac")
PY

DWS_EXECUTABLE=$("$PYTHON" - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path

value = "dws"
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8-sig").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, candidate = line.split("=", 1)
    if key.strip() == "DINGTALK_DWS_EXECUTABLE" and candidate.strip():
        value = candidate.strip()
        break
print(value)
PY
)
command -v "$DWS_EXECUTABLE" >/dev/null 2>&1 \
  || [ -x "$DWS_EXECUTABLE" ] \
  || fail "DWS executable is unavailable: $DWS_EXECUTABLE"
pass "DWS executable is available"

if command -v openclaw >/dev/null 2>&1; then
  pass "OpenClaw executable is available"
else
  printf 'WARN: OpenClaw is unavailable; keep OPENCLAW_SHAREHOLDER_ENABLED=false\n'
fi

mkdir -p "$PROJECT_ROOT/.runtime/customer-service"
[ -w "$PROJECT_ROOT/.runtime/customer-service" ] \
  || fail "runtime directory is not writable"
pass "runtime directory is writable"

printf '\nPreflight completed. No service was started and no secret was printed.\n'
