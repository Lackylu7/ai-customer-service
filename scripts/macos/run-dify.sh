#!/bin/bash
set -eu

ACTION="${1:-start}"
VERSION="${2:-${DIFY_VERSION:-1.15.0}}"

case "$ACTION" in
  start|status|stop|logs)
    ;;
  *)
    echo "Usage: $0 {start|status|stop|logs} [version]" >&2
    exit 2
    ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
DOCKER_ROOT="$PROJECT_ROOT/.runtime/dify-$VERSION/docker"
COMPOSE_PATH="$DOCKER_ROOT/docker-compose.yaml"
OVERRIDE_PATH="$PROJECT_ROOT/infra/dify/docker-compose.local.yaml"
ENV_PATH="$DOCKER_ROOT/.env"

if [ ! -f "$ENV_PATH" ]; then
  echo "ERROR: Dify is not configured. Run scripts/macos/setup-dify.sh first." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not reachable." >&2
  exit 1
fi

compose() {
  docker compose --project-directory "$DOCKER_ROOT" --env-file "$ENV_PATH" \
    -f "$COMPOSE_PATH" -f "$OVERRIDE_PATH" "$@"
}

case "$ACTION" in
  start)
    compose up -d
    ;;
  status)
    compose ps
    ;;
  stop)
    compose down
    ;;
  logs)
    compose logs --tail 200
    ;;
esac
