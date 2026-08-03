#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ENV="${PROJECT_ENV:-$PROJECT_ROOT/.env.production}"
[[ -f "$PROJECT_ENV" ]] || { echo "Missing project environment file." >&2; exit 1; }

read_env() {
    local key="$1"
    local value
    value="$(grep -E "^${key}=" "$PROJECT_ENV" | tail -n 1 | cut -d= -f2-)"
    [[ -n "$value" ]] || { echo "Missing $key in project environment." >&2; exit 1; }
    printf '%s' "$value"
}

operations_domain="$(read_env OPERATIONS_DOMAIN)"
support_domain="$(read_env SUPPORT_DOMAIN)"

check_url() {
    local name="$1"
    local url="$2"
    curl --fail --silent --show-error --max-time 15 "$url" >/dev/null
    echo "PASS: $name"
}

check_url "AI liveness" "https://${operations_domain}/health"
check_url "AI readiness and worker heartbeat" "https://${operations_domain}/ready"
check_url "Chatwoot API" "https://${support_domain}/api"
check_url "Dify console API" "http://127.0.0.1:8081/console/api/setup"

docker compose --env-file "$PROJECT_ENV" \
    -f "$PROJECT_ROOT/compose.yaml" -f "$PROJECT_ROOT/compose.production.yaml" ps
