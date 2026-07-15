param(
    [ValidateSet("start", "status", "stop", "logs")]
    [string]$Action = "start",
    [string]$Version = "1.15.0"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dockerRoot = Join-Path $projectRoot ".runtime\dify-$Version\docker"
$composePath = Join-Path $dockerRoot "docker-compose.yaml"
$overridePath = Join-Path $projectRoot "infra\dify\docker-compose.local.yaml"
$envPath = Join-Path $dockerRoot ".env"

if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Dify is not configured. Run .\scripts\setup-dify.ps1 first."
}

switch ($Action) {
    "start" {
        docker compose --project-directory $dockerRoot --env-file $envPath `
            -f $composePath -f $overridePath up -d
    }
    "status" {
        docker compose --project-directory $dockerRoot --env-file $envPath `
            -f $composePath -f $overridePath ps
    }
    "stop" {
        docker compose --project-directory $dockerRoot --env-file $envPath `
            -f $composePath -f $overridePath down
    }
    "logs" {
        docker compose --project-directory $dockerRoot --env-file $envPath `
            -f $composePath -f $overridePath logs --tail 200
    }
}

if ($LASTEXITCODE -ne 0) {
    throw "Dify action failed: $Action"
}
