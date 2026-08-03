#!/bin/bash
set -eu

if [ "$(uname -s)" != "Darwin" ]; then
  echo "ERROR: this preflight must run on macOS." >&2
  exit 1
fi

echo "=== Host ==="
echo "hostname: $(hostname)"
echo "user: $(id -un)"
echo "architecture: $(uname -m)"
sw_vers
df -h "$HOME" | tail -n 1

echo "=== Required tools ==="
missing_required=0
for command_name in git python3 docker curl lsof; do
  if command -v "$command_name" >/dev/null 2>&1; then
    echo "OK: $command_name -> $(command -v "$command_name")"
  else
    echo "MISSING: $command_name"
    missing_required=1
  fi
done

if command -v python3 >/dev/null 2>&1; then
  if python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  then
    echo "OK: Python $(python3 -c 'import platform; print(platform.python_version())') is supported"
  else
    echo "UNSUPPORTED: Python $(python3 -c 'import platform; print(platform.python_version())'); Python 3.10+ is required"
    missing_required=1
  fi
fi

echo "=== Optional integration tools ==="
for command_name in colima dws openclaw; do
  if command -v "$command_name" >/dev/null 2>&1; then
    echo "OK: $command_name -> $(command -v "$command_name")"
  else
    echo "NOT INSTALLED: $command_name"
  fi
done

echo "=== Docker ==="
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    echo "OK: Docker daemon is reachable"
    if docker compose version >/dev/null 2>&1; then
      docker compose version
    else
      echo "MISSING: Docker Compose v2 plugin"
      missing_required=1
    fi
  else
    echo "NOT READY: Docker is installed but its daemon is not reachable"
    echo "If this Mac uses Colima, run: colima start"
  fi
fi

echo "=== Relevant listening ports ==="
for port in 8000 8081 8443 18789 18791; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +2 | grep -q .; then
    echo "IN USE: $port"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +2
  else
    echo "FREE: $port"
  fi
done

if [ "$missing_required" -ne 0 ]; then
  echo "Host preflight found missing required tools." >&2
  exit 2
fi

echo "Host preflight completed. No services were changed."
