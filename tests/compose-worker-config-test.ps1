$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $rendered = docker compose config --format json
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose configuration could not be rendered."
    }
    $config = $rendered | ConvertFrom-Json
    if (($config.services.worker.command -join " ") -notmatch "python -m app.worker") {
        throw "Worker service command is missing."
    }
    if ($config.services.worker.ports) {
        throw "Worker service must not publish ports."
    }
    if (-not $config.services.worker.volumes) {
        throw "Worker service must share the durable SQLite volume."
    }
    $apiVolumes = @($config.services.docling.volumes | ForEach-Object { $_.source, $_.target -join " -> " })
    $workerVolumes = @($config.services.worker.volumes | ForEach-Object { $_.source, $_.target -join " -> " })
    if (-not (@($workerVolumes | Where-Object { $apiVolumes -contains $_ }).Count)) {
        throw "API and Worker do not share the durable SQLite volume."
    }
    Write-Output "PASS: Compose runs API and Worker with shared durable storage."
}
finally {
    Pop-Location
}
