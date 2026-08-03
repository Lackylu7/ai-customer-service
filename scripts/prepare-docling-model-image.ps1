param(
    [string]$ImageTag = "ai-customer-service/docling-models:2.112.0"
)

$ErrorActionPreference = "Stop"

if ($ImageTag -notmatch '^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*$') {
    throw "ImageTag contains unsupported characters."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$modelRoot = Join-Path $projectRoot ".runtime\docling-models"
$prepareScript = Join-Path $PSScriptRoot "prepare_docling_models.py"
$verifyScript = Join-Path $PSScriptRoot "verify_docling_models.py"

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

& $python $prepareScript --root $modelRoot
if ($LASTEXITCODE -ne 0) {
    throw "Docling model preparation failed."
}

& $python $verifyScript --root $modelRoot
if ($LASTEXITCODE -ne 0) {
    throw "Docling model verification failed."
}

Push-Location $projectRoot
try {
    # PowerShell 5 corrupts binary data in native pipelines, so cmd.exe owns
    # the tar-to-Docker binary stream. Paths are fixed and ImageTag is validated.
    $importCommand = "tar.exe -C `".runtime\docling-models`" -cf - . | docker.exe import --platform linux/amd64 - $ImageTag"
    & cmd.exe /d /c $importCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Docker could not import the verified Docling model layer."
    }

    docker image inspect $ImageTag --format '{{.Id}} {{.Size}} {{.Os}}/{{.Architecture}}'
    if ($LASTEXITCODE -ne 0) {
        throw "The imported Docling model image could not be inspected."
    }
}
finally {
    Pop-Location
}
