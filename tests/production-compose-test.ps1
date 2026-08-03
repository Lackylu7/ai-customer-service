$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$productionEnv = Join-Path $projectRoot ".env.production.example"
$productionCompose = Join-Path $projectRoot "compose.production.yaml"
$caddyfile = Join-Path $projectRoot "infra/caddy/Caddyfile"

foreach ($path in @($productionEnv, $productionCompose, $caddyfile)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing production deployment file: $path"
    }
}

Push-Location $projectRoot
try {
    $rendered = docker compose `
        --env-file $productionEnv `
        -f compose.yaml `
        -f compose.production.yaml `
        config --format json
    if ($LASTEXITCODE -ne 0) {
        throw "Production Docker Compose configuration could not be rendered."
    }
    $config = $rendered | ConvertFrom-Json
}
finally {
    Pop-Location
}

if ([string]$config.services.docling.environment.APP_ENV -ne "production") {
    throw "The API is not forced into production mode."
}
if ([string]$config.services.docling.environment.APP_ALLOWED_HOSTS -ne "ops.example.com") {
    throw "The API does not receive the production operations host."
}
if ([string]$config.services.docling.environment.CHATWOOT_PUBLIC_BASE_URL -ne "https://support.example.com") {
    throw "The browser-safe Chatwoot production URL is incorrect."
}
foreach ($publishedPort in @($config.services.docling.ports)) {
    if ($publishedPort.host_ip -ne "127.0.0.1") {
        throw "The application API must not be published directly to the Internet."
    }
}
foreach ($serviceName in @("docling", "worker")) {
    $service = $config.services.$serviceName
    if ($service.read_only -ne $true) {
        throw "$serviceName root filesystem is not read-only in production."
    }
    if ("ALL" -notin @($service.cap_drop)) {
        throw "$serviceName Linux capabilities are not dropped in production."
    }
    if ("no-new-privileges:true" -notin @($service.security_opt)) {
        throw "$serviceName can gain additional privileges in production."
    }
}

$caddy = $config.services.caddy
if ([string]$caddy.image -ne "caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648") {
    throw "Caddy must be pinned to the reviewed production image."
}
$published = @($caddy.ports | ForEach-Object { "$($_.published)/$($_.protocol)" })
foreach ($requiredPort in @("80/tcp", "443/tcp", "443/udp")) {
    if ($requiredPort -notin $published) {
        throw "Caddy is missing public port $requiredPort."
    }
}
$caddyTargets = @($caddy.volumes | ForEach-Object { $_.target })
foreach ($requiredTarget in @("/data", "/config", "/etc/caddy")) {
    if ($requiredTarget -notin $caddyTargets) {
        throw "Caddy is missing durable/config volume $requiredTarget."
    }
}

$caddyText = Get-Content -LiteralPath $caddyfile -Raw -Encoding UTF8
foreach ($requiredText in @(
    '{$SUPPORT_DOMAIN}',
    '{$OPERATIONS_DOMAIN}',
    "reverse_proxy host.docker.internal:3000",
    "reverse_proxy docling:5001",
    "max_size 32MB"
)) {
    if ($caddyText -notmatch [Regex]::Escape($requiredText)) {
        throw "Caddyfile is missing required production routing: $requiredText"
    }
}

$envText = Get-Content -LiteralPath $productionEnv -Raw -Encoding UTF8
if ($envText -match 'sk-[A-Za-z0-9]' -or $envText -match 'Bearer\s+[A-Za-z0-9]') {
    throw "Production environment example contains credential-like content."
}

Write-Output "PASS: Production routing exposes only HTTPS entrypoints and keeps secrets out of templates."
