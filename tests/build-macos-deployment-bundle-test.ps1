$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$scriptPath = Join-Path $projectRoot "scripts\build-macos-deployment-bundle.ps1"
$content = Get-Content -LiteralPath $scriptPath -Raw -Encoding utf8

foreach ($required in @(
    "deployment-manifest.json",
    "knowledge-approved-sources",
    "shareholder-3-0-openclaw-package-v0.4.zip",
    "upgrade-macos.command",
    "System.IO.Compression.ZipFile"
)) {
    if (-not $content.Contains($required)) {
        throw "Mac deployment bundle script is missing: $required"
    }
}

foreach ($forbidden in @(
    'Copy-Item -LiteralPath (Join-Path $projectRoot ".env")',
    'Copy-Item -LiteralPath (Join-Path $projectRoot ".runtime")',
    'Copy-Item -LiteralPath (Join-Path $projectRoot ".venv")'
)) {
    if ($content.Contains($forbidden)) {
        throw "Mac deployment bundle script includes forbidden local data: $forbidden"
    }
}

$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath,
    [ref]$tokens,
    [ref]$errors
) | Out-Null
if ($errors.Count -gt 0) {
    throw ($errors | ForEach-Object Message | Out-String)
}

Write-Host "Mac deployment bundle script checks passed."
