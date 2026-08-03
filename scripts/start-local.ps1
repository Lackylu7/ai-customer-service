param(
    [string]$ConfigPath = "config\enterprise.example.json",
    [string]$EnvPath = ".env",
    [string]$ChatwootPath = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runDifyPath = Join-Path $PSScriptRoot "run-dify.ps1"
$runWidgetPath = Join-Path $PSScriptRoot "run-widget-demo.ps1"
$statusPath = Join-Path $PSScriptRoot "status-local.ps1"
$prepareModelImagePath = Join-Path $PSScriptRoot "prepare-docling-model-image.ps1"
if ([string]::IsNullOrWhiteSpace($ChatwootPath)) {
    $ChatwootPath = Join-Path (Split-Path -Parent $projectRoot) "chatwoot-local"
}
if (-not [IO.Path]::IsPathRooted($EnvPath)) {
    $resolvedEnvPath = [IO.Path]::GetFullPath((Join-Path $projectRoot $EnvPath))
}
else {
    $resolvedEnvPath = [IO.Path]::GetFullPath($EnvPath)
}
if (-not (Test-Path -LiteralPath $resolvedEnvPath -PathType Leaf)) {
    throw "Local .env file does not exist."
}
if (-not (Test-Path -LiteralPath $ChatwootPath -PathType Container)) {
    throw "Chatwoot project directory does not exist."
}

if (-not $SkipBuild.IsPresent) {
    & $prepareModelImagePath
}

& $runDifyPath start
& docker compose --project-directory $ChatwootPath up -d
if ($LASTEXITCODE -ne 0) { throw "Chatwoot failed to start." }

$composeArguments = @(
    "compose",
    "--project-directory", $projectRoot,
    "--env-file", $resolvedEnvPath,
    "up", "-d"
)
if (-not $SkipBuild.IsPresent) { $composeArguments += "--build" }
$composeArguments += @("docling", "worker")
& docker @composeArguments
if ($LASTEXITCODE -ne 0) { throw "AI service failed to start." }

& $runWidgetPath -ConfigPath $ConfigPath -EnvPath $resolvedEnvPath
& $statusPath
