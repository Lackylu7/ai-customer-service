#!/bin/bash
set -eu

VERSION="${DIFY_VERSION:-1.15.0}"
HTTP_PORT="${DIFY_HTTP_PORT:-8081}"
HTTPS_PORT="${DIFY_HTTPS_PORT:-8443}"
WORKER_AMOUNT="${DIFY_WORKER_AMOUNT:-1}"
START_AFTER_SETUP=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --start)
      START_AFTER_SETUP=1
      ;;
    --version)
      shift
      VERSION="${1:?missing value for --version}"
      ;;
    --http-port)
      shift
      HTTP_PORT="${1:?missing value for --http-port}"
      ;;
    --https-port)
      shift
      HTTPS_PORT="${1:?missing value for --https-port}"
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "ERROR: this installer must run on macOS." >&2
  exit 1
fi

for command_name in curl python3 docker; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "ERROR: required command is missing: $command_name" >&2
    exit 1
  fi
done

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not reachable. Start Docker or run 'colima start'." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 is required." >&2
  exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
RUNTIME_ROOT="$PROJECT_ROOT/.runtime"
DIFY_ROOT="$RUNTIME_ROOT/dify-$VERSION"
DOCKER_ROOT="$DIFY_ROOT/docker"
COMPOSE_PATH="$DOCKER_ROOT/docker-compose.yaml"
OVERRIDE_PATH="$PROJECT_ROOT/infra/dify/docker-compose.local.yaml"
ENV_EXAMPLE_PATH="$DOCKER_ROOT/.env.example"
ENV_PATH="$DOCKER_ROOT/.env"

mkdir -p "$RUNTIME_ROOT"
if [ ! -e "$DIFY_ROOT" ]; then
  DOWNLOAD_ROOT=$(mktemp -d "$RUNTIME_ROOT/dify-download.XXXXXX")
  ARCHIVE_PATH="$DOWNLOAD_ROOT/dify-$VERSION.tar.gz"
  EXTRACT_ROOT="$DOWNLOAD_ROOT/source"
  mkdir -p "$EXTRACT_ROOT"
  cleanup_download() {
    rm -rf "$DOWNLOAD_ROOT"
  }
  trap cleanup_download EXIT INT TERM

  curl --http1.1 --fail --location \
    --retry 5 --retry-delay 3 --retry-all-errors \
    "https://github.com/langgenius/dify/archive/refs/tags/$VERSION.tar.gz" \
    --output "$ARCHIVE_PATH"
  tar -xzf "$ARCHIVE_PATH" -C "$EXTRACT_ROOT"
  EXTRACTED_ROOT="$EXTRACT_ROOT/dify-$VERSION"
  if [ ! -f "$EXTRACTED_ROOT/docker/.env.example" ] || \
     [ ! -f "$EXTRACTED_ROOT/docker/docker-compose.yaml" ]; then
    echo "ERROR: downloaded Dify $VERSION archive is incomplete." >&2
    exit 1
  fi
  mv "$EXTRACTED_ROOT" "$DIFY_ROOT"
  cleanup_download
  trap - EXIT INT TERM
fi

if [ ! -f "$ENV_EXAMPLE_PATH" ] || [ ! -f "$COMPOSE_PATH" ]; then
  echo "ERROR: Dify $VERSION is incomplete at $DIFY_ROOT" >&2
  exit 1
fi

if [ -f "$ENV_PATH" ]; then
  echo "Dify .env already exists; keeping the existing configuration."
else
  cp "$ENV_EXAMPLE_PATH" "$ENV_PATH"
  DIFY_ENV_PATH="$ENV_PATH" \
  DIFY_HTTP_PORT_VALUE="$HTTP_PORT" \
  DIFY_HTTPS_PORT_VALUE="$HTTPS_PORT" \
  DIFY_WORKER_AMOUNT_VALUE="$WORKER_AMOUNT" \
  python3 - <<'PY'
from pathlib import Path
import os

path = Path(os.environ["DIFY_ENV_PATH"])
text = path.read_text(encoding="utf-8")
updates = {
    "EXPOSE_NGINX_PORT": os.environ["DIFY_HTTP_PORT_VALUE"],
    "EXPOSE_NGINX_SSL_PORT": os.environ["DIFY_HTTPS_PORT_VALUE"],
    "CELERY_WORKER_AMOUNT": os.environ["DIFY_WORKER_AMOUNT_VALUE"],
    "COMPOSE_PROFILES": "${VECTOR_STORE:-weaviate},${DB_TYPE:-postgresql}",
}
lines = text.splitlines()
found = set()
for index, line in enumerate(lines):
    if "=" not in line or line.lstrip().startswith("#"):
        continue
    name = line.split("=", 1)[0]
    if name in updates:
        lines[index] = f"{name}={updates[name]}"
        found.add(name)
missing = sorted(set(updates) - found)
if missing:
    raise SystemExit(f"Dify environment variables not found: {', '.join(missing)}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  chmod 600 "$ENV_PATH"
fi

docker compose --project-directory "$DOCKER_ROOT" --env-file "$ENV_PATH" \
  -f "$COMPOSE_PATH" -f "$OVERRIDE_PATH" config --quiet

echo "Dify $VERSION is configured at: $DIFY_ROOT"
echo "Local URL after startup: http://127.0.0.1:$HTTP_PORT"

if [ "$START_AFTER_SETUP" -eq 1 ]; then
  "$SCRIPT_DIR/run-dify.sh" start "$VERSION"
else
  echo "Dify was not started. Run: scripts/macos/run-dify.sh start $VERSION"
fi
