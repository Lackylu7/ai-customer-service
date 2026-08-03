#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ "${1:-}" != "--backup" || -z "${2:-}" || "${3:-}" != "--confirm" || "${4:-}" != "RESTORE-PRODUCTION" ]]; then
    echo "Usage: $0 --backup /path/backup.tar.gz.gpg --confirm RESTORE-PRODUCTION" >&2
    exit 2
fi

backup_file="$(realpath "$2")"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ENV="${PROJECT_ENV:-$PROJECT_ROOT/.env.production}"
BACKUP_PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:?BACKUP_PASSPHRASE_FILE is required}"
DIFY_COMPOSE_DIR="${DIFY_COMPOSE_DIR:?DIFY_COMPOSE_DIR is required}"
CHATWOOT_COMPOSE_DIR="${CHATWOOT_COMPOSE_DIR:?CHATWOOT_COMPOSE_DIR is required}"

for command_name in docker tar gpg sha256sum realpath mktemp gzip grep; do
    command -v "$command_name" >/dev/null || { echo "Missing required command: $command_name" >&2; exit 1; }
done
[[ -f "$backup_file" && -f "$backup_file.sha256" ]] || { echo "Backup or checksum is missing." >&2; exit 1; }
[[ -r "$BACKUP_PASSPHRASE_FILE" ]] || { echo "Backup passphrase file is not readable." >&2; exit 1; }
[[ -f "$PROJECT_ENV" ]] || { echo "Missing project environment file." >&2; exit 1; }
[[ -f "$DIFY_COMPOSE_DIR/docker-compose.yaml" ]] || { echo "Invalid Dify Compose directory." >&2; exit 1; }
[[ -f "$CHATWOOT_COMPOSE_DIR/docker-compose.yaml" ]] || { echo "Invalid Chatwoot Compose directory." >&2; exit 1; }

(cd "$(dirname "$backup_file")" && sha256sum --check "$(basename "$backup_file").sha256")
staging="$(mktemp -d)"
cleanup() {
    if [[ -d "$staging" && "$staging" == /tmp/* ]]; then
        rm -rf -- "$staging"
    fi
}
trap cleanup EXIT

gpg --batch --yes --decrypt --passphrase-file "$BACKUP_PASSPHRASE_FILE" \
    --output "$staging/bundle.tar.gz" "$backup_file"
if tar -tzf "$staging/bundle.tar.gz" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    echo "Unsafe path found in backup archive." >&2
    exit 1
fi
tar -C "$staging" -xzf "$staging/bundle.tar.gz"
(cd "$staging/payload" && sha256sum --check MANIFEST.sha256)

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
project_compose=(docker compose --env-file "$PROJECT_ENV" -f "$PROJECT_ROOT/compose.yaml" -f "$PROJECT_ROOT/compose.production.yaml")
"${project_compose[@]}" down
(cd "$CHATWOOT_COMPOSE_DIR" && docker compose stop)
(cd "$DIFY_COMPOSE_DIR" && docker compose stop)

state_dir="$PROJECT_ROOT/.runtime/customer-service"
mkdir -p "$state_dir"
if [[ -e "$state_dir/state.db" ]]; then
    mv "$state_dir/state.db" "$state_dir/state.db.pre-restore-${timestamp}"
fi
cp "$staging/payload/customer-service/state.db" "$state_dir/restore-source.db"
"${project_compose[@]}" run --rm --no-deps -T docling \
    python -m app.backup restore \
    --backup /app/.runtime/customer-service/restore-source.db \
    --target /app/.runtime/customer-service/state.db \
    --confirm RESTORE >/dev/null
rm -f -- "$state_dir/restore-source.db"

if [[ -e "$DIFY_COMPOSE_DIR/volumes" ]]; then
    mv "$DIFY_COMPOSE_DIR/volumes" "$DIFY_COMPOSE_DIR/volumes.pre-restore-${timestamp}"
fi
tar -C "$DIFY_COMPOSE_DIR" -xzf "$staging/payload/dify/dify-volumes.tar.gz"

(cd "$CHATWOOT_COMPOSE_DIR" && docker compose up -d postgres)
cat "$staging/payload/chatwoot/postgres.dump" | \
    (cd "$CHATWOOT_COMPOSE_DIR" && docker compose exec -T postgres \
        sh -lc 'pg_restore --clean --if-exists --no-owner -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-chatwoot}"')
(cd "$CHATWOOT_COMPOSE_DIR" && docker compose run --rm --no-deps -T base \
    sh -lc "tar -C /app/storage -czf - .") \
    > "$CHATWOOT_COMPOSE_DIR/storage.pre-restore-${timestamp}.tar.gz"
gzip -dc "$staging/payload/chatwoot/storage.tar.gz" | \
    (cd "$CHATWOOT_COMPOSE_DIR" && docker compose run --rm --no-deps -T base \
        sh -lc 'find /app/storage -mindepth 1 -delete && tar -C /app/storage -xf -')

(cd "$DIFY_COMPOSE_DIR" && docker compose up -d)
(cd "$CHATWOOT_COMPOSE_DIR" && docker compose up -d)
"${project_compose[@]}" up -d
echo "Restore completed. Run scripts/production/health-check.sh before reopening traffic."
