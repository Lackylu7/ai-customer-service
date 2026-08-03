$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$requiredScripts = @(
    "scripts/production/backup-production.sh",
    "scripts/production/restore-production.sh",
    "scripts/production/health-check.sh"
)
foreach ($relativePath in $requiredScripts) {
    $path = Join-Path $projectRoot $relativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing production operations script: $relativePath"
    }
    $content = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    if ($content -notmatch 'set -Eeuo pipefail') {
        throw "$relativePath does not fail safely."
    }
}

$backup = Get-Content -LiteralPath (Join-Path $projectRoot $requiredScripts[0]) -Raw -Encoding UTF8
foreach ($requiredText in @(
    "umask 077",
    "python -m app.backup backup",
    "pg_dump",
    "/app/storage",
    "dify-volumes.tar.gz",
    "gpg --batch",
    "sha256sum"
)) {
    if ($backup -notmatch [Regex]::Escape($requiredText)) {
        throw "Production backup is missing: $requiredText"
    }
}

$restore = Get-Content -LiteralPath (Join-Path $projectRoot $requiredScripts[1]) -Raw -Encoding UTF8
foreach ($requiredText in @(
    "RESTORE-PRODUCTION",
    "sha256sum --check",
    "gpg --batch",
    "pg_restore",
    "pre-restore",
    "python -m app.backup restore"
)) {
    if ($restore -notmatch [Regex]::Escape($requiredText)) {
        throw "Production restore is missing: $requiredText"
    }
}
if ($restore -notmatch 'for command_name in[^\n]*gzip') {
    throw "Production restore does not preflight its gzip dependency."
}

$health = Get-Content -LiteralPath (Join-Path $projectRoot $requiredScripts[2]) -Raw -Encoding UTF8
foreach ($requiredText in @(
    '/health',
    '/ready',
    '/api',
    '/console/api/setup'
)) {
    if ($health -notmatch [Regex]::Escape($requiredText)) {
        throw "Production health check is missing: $requiredText"
    }
}

$exportPath = Join-Path $projectRoot "scripts\export-production-images.ps1"
if (-not (Test-Path -LiteralPath $exportPath -PathType Leaf)) {
    throw "Missing production image export script."
}
$export = Get-Content -LiteralPath $exportPath -Raw -Encoding UTF8
foreach ($requiredText in @(
    "docker image inspect",
    "docker image save",
    "ai-customer-service/docling-api:0.1.0",
    "caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648",
    "Get-FileHash"
)) {
    if ($export -notmatch [Regex]::Escape($requiredText)) {
        throw "Production image export is missing: $requiredText"
    }
}

Write-Output "PASS: Production backup, guarded restore, and health checks are present."
