#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ENV="${PROJECT_ENV:-$PROJECT_ROOT/.env.production}"
BACKUP_ROOT="${BACKUP_ROOT:?BACKUP_ROOT must point to an off-server or separately replicated mount}"
BACKUP_PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:?BACKUP_PASSPHRASE_FILE is required}"
DIFY_COMPOSE_DIR="${DIFY_COMPOSE_DIR:?DIFY_COMPOSE_DIR is required}"
CHATWOOT_COMPOSE_DIR="${CHATWOOT_COMPOSE_DIR:?CHATWOOT_COMPOSE_DIR is required}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

for command_name in docker tar gpg sha256sum install find; do
    command -v "$command_name" >/dev/null || {
        echo "Missing required command: $command_name" >&2
        exit 1
    }
done
[[ -f "$PROJECT_ENV" ]] || { echo "Missing project environment file." >&2; exit 1; }
[[ -r "$BACKUP_PASSPHRASE_FILE" ]] || { echo "Backup passphrase file is not readable." >&2; exit 1; }
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || { echo "BACKUP_RETENTION_DAYS must be numeric." >&2; exit 1; }
[[ -f "$DIFY_COMPOSE_DIR/docker-compose.yaml" ]] || { echo "Invalid Dify Compose directory." >&2; exit 1; }
[[ -f "$CHATWOOT_COMPOSE_DIR/docker-compose.yaml" ]] || { echo "Invalid Chatwoot Compose directory." >&2; exit 1; }

install -d -m 700 "$BACKUP_ROOT"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging="$(mktemp -d "$BACKUP_ROOT/.partial-${timestamp}-XXXXXX")"
payload="$staging/payload"
mkdir -p "$payload/customer-service" "$payload/chatwoot" "$payload/dify" "$payload/config"

dify_stopped=0
chatwoot_stopped=0
cleanup() {
    if [[ "$chatwoot_stopped" == 1 ]]; then
        (cd "$CHATWOOT_COMPOSE_DIR" && docker compose start rails sidekiq) >/dev/null 2>&1 || true
    fi
    if [[ "$dify_stopped" == 1 ]]; then
        (cd "$DIFY_COMPOSE_DIR" && docker compose up -d) >/dev/null 2>&1 || true
    fi
    if [[ -d "$staging" && "$staging" == "$BACKUP_ROOT"/.partial-* ]]; then
        rm -rf -- "$staging"
    fi
}
trap cleanup EXIT

project_compose=(docker compose --env-file "$PROJECT_ENV" -f "$PROJECT_ROOT/compose.yaml" -f "$PROJECT_ROOT/compose.production.yaml")
"${project_compose[@]}" exec -T docling \
    python -m app.backup backup \
    --source /app/.runtime/customer-service/state.db \
    --destination "/app/.runtime/customer-service/backups/state-${timestamp}.db" \
    >/dev/null
cp "$PROJECT_ROOT/.runtime/customer-service/backups/state-${timestamp}.db" "$payload/customer-service/state.db"

(cd "$CHATWOOT_COMPOSE_DIR" && docker compose exec -T postgres \
    sh -lc 'pg_dump -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-chatwoot}" -Fc') \
    > "$payload/chatwoot/postgres.dump"
(cd "$CHATWOOT_COMPOSE_DIR" && docker compose stop rails sidekiq)
chatwoot_stopped=1
(cd "$CHATWOOT_COMPOSE_DIR" && docker compose run --rm --no-deps -T base \
    sh -lc 'tar -C /app/storage -czf - .') > "$payload/chatwoot/storage.tar.gz"
tar -C "$CHATWOOT_COMPOSE_DIR" -czf "$payload/chatwoot/config.tar.gz" .env docker-compose.yaml
(cd "$CHATWOOT_COMPOSE_DIR" && docker compose start rails sidekiq)
chatwoot_stopped=0

(cd "$DIFY_COMPOSE_DIR" && docker compose stop)
dify_stopped=1
tar -C "$DIFY_COMPOSE_DIR" -czf "$payload/dify/dify-volumes.tar.gz" volumes
tar -C "$DIFY_COMPOSE_DIR" -czf "$payload/dify/config.tar.gz" .env docker-compose.yaml
(cd "$DIFY_COMPOSE_DIR" && docker compose up -d)
dify_stopped=0

tar -C "$PROJECT_ROOT" -czf "$payload/config/project-config.tar.gz" \
    .env.production compose.yaml compose.production.yaml config/enterprise.local.json infra/caddy/Caddyfile
(cd "$payload" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256)

plain_archive="$staging/ai-customer-service-${timestamp}.tar.gz"
encrypted_archive="$BACKUP_ROOT/ai-customer-service-${timestamp}.tar.gz.gpg"
tar -C "$staging" -czf "$plain_archive" payload
gpg --batch --yes --symmetric --cipher-algo AES256 \
    --s2k-digest-algo SHA512 --s2k-count 65011712 \
    --passphrase-file "$BACKUP_PASSPHRASE_FILE" \
    --output "$encrypted_archive" "$plain_archive"
(cd "$BACKUP_ROOT" && sha256sum "$(basename "$encrypted_archive")" > "$(basename "$encrypted_archive").sha256")

find "$BACKUP_ROOT" -maxdepth 1 -type f \
    -name 'ai-customer-service-*.tar.gz.gpg*' -mtime "+$RETENTION_DAYS" -delete
echo "Backup completed: $encrypted_archive"
