$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$dockerfile = Get-Content -LiteralPath (Join-Path $projectRoot "Dockerfile") -Raw -Encoding UTF8
foreach ($requiredText in @(
    "libxcb1",
    "libgl1",
    "libglib2.0-0t64",
    "FROM ai-customer-service/docling-models:2.112.0 AS docling_models",
    "FROM python:3.12-slim@sha256:",
    "COPY --from=docling_models",
    "scripts/verify_docling_models.py",
    "DOCLING_ARTIFACTS_PATH=/opt/docling-models",
    "USER 10001:10001",
    '"--no-server-header"'
)) {
    if ($dockerfile -notmatch [Regex]::Escape($requiredText)) {
        throw "Dockerfile does not pre-package the required Docling model assets: $requiredText"
    }
}

Push-Location $projectRoot
try {
    $rendered = docker compose config --format json
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose configuration could not be rendered."
    }
    $config = $rendered | ConvertFrom-Json
    foreach ($serviceName in @("docling", "worker")) {
        $artifactsPath = [string]$config.services.$serviceName.environment.DOCLING_ARTIFACTS_PATH
        if ($artifactsPath -ne "/opt/docling-models") {
            throw "$serviceName does not use the pre-packaged Docling model assets."
        }
        if ($null -ne $config.services.$serviceName.build.additional_contexts) {
            throw "$serviceName still sends the large model directory through BuildKit."
        }
    }
}
finally {
    Pop-Location
}

$smokeScript = Get-Content -LiteralPath (Join-Path $PSScriptRoot "docling-smoke.ps1") -Raw -Encoding UTF8
if ($smokeScript -notmatch 'Authorization.*Bearer') {
    throw "The HTML smoke test does not authenticate with the protected conversion API."
}
if ($smokeScript -match 'X-Api-Key') {
    throw "The HTML smoke test still uses the retired Docling API header."
}

$pdfSmoke = Get-Content -LiteralPath (Join-Path $PSScriptRoot "docling-pdf-smoke.py") -Raw -Encoding UTF8
if ($pdfSmoke -notmatch 'Authorization.*Bearer') {
    throw "The PDF smoke test does not authenticate with the protected conversion API."
}

if ($dockerfile -match 'docling-tools models download') {
    throw "Dockerfile still downloads Docling models during the production image build."
}

$prepareScriptPath = Join-Path $projectRoot "scripts\prepare-docling-model-image.ps1"
if (-not (Test-Path -LiteralPath $prepareScriptPath -PathType Leaf)) {
    throw "The reproducible Docling model image preparation script is missing."
}
$prepareScript = Get-Content -LiteralPath $prepareScriptPath -Raw -Encoding UTF8
foreach ($requiredText in @(
    "prepare_docling_models.py",
    "verify_docling_models.py",
    "cmd.exe",
    "docker.exe import --platform linux/amd64",
    "ai-customer-service/docling-models:2.112.0"
)) {
    if ($prepareScript -notmatch [Regex]::Escape($requiredText)) {
        throw "The Docling model image preparation script is incomplete: $requiredText"
    }
}

$startLocal = Get-Content -LiteralPath (Join-Path $projectRoot "scripts\start-local.ps1") -Raw -Encoding UTF8
if ($startLocal -notmatch [Regex]::Escape("prepare-docling-model-image.ps1")) {
    throw "Local Docker startup does not prepare the verified Docling model layer before building."
}

Write-Output "PASS: Docling models are packaged and smoke tests use admin authentication."
