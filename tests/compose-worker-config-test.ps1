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
    foreach ($serviceName in @("docling", "worker")) {
        $environment = $config.services.$serviceName.environment
        if ($null -eq $environment.CUSTOMER_SERVICE_HUMAN_SLA_SECONDS) {
            throw "$serviceName compose config does not expose the human SLA setting."
        }
        if ($null -eq $environment.CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS) {
            throw "$serviceName compose config does not expose the fallback cooldown setting."
        }
    }

    $readme = Get-Content -LiteralPath (Join-Path $projectRoot "README.md") -Raw -Encoding UTF8
    foreach ($requiredText in @(
        [Regex]::Unescape('AI\u0020\u4e34\u65f6\u534f\u52a9'),
        [Regex]::Unescape('\u516c\u5f00\u56de\u590d'),
        [Regex]::Unescape('\u65e7\u6d88\u606f\u4e0d\u4f1a\u8865\u7b54'),
        "reset-ai-conversation.ps1",
        "CUSTOMER_SERVICE_HUMAN_SLA_SECONDS",
        "CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS"
    )) {
        if ($readme -notmatch [Regex]::Escape($requiredText)) {
            throw "README is missing the required human-handoff guidance: $requiredText"
        }
    }
    Write-Output "PASS: Compose and README expose durable human-handoff operations."
}
finally {
    Pop-Location
}
