$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot "scripts\configure-operations.ps1"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempRoot = Join-Path $tempBase ("operations-env-test-" + [Guid]::NewGuid().ToString("N"))
$envPath = Join-Path $tempRoot ".env"

function Get-OperationsKey {
    param([string]$Path)

    $line = [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8) |
        Where-Object { $_.StartsWith("OPERATIONS_API_KEY=", [StringComparison]::Ordinal) } |
        Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($line)) {
        throw "Operations key was not written."
    }
    return $line.Substring("OPERATIONS_API_KEY=".Length)
}

New-Item -ItemType Directory -Path $tempRoot | Out-Null

try {
    @(
        "DIFY_BASE_URL=http://127.0.0.1:8081"
        "UNRELATED_SETTING=keep-me"
        "OPERATIONS_API_KEY="
    ) | Set-Content -LiteralPath $envPath -Encoding UTF8

    $firstOutput = & $scriptPath -EnvPath $envPath 6>&1 | Out-String
    $firstKey = Get-OperationsKey -Path $envPath
    if ($firstKey.Length -lt 43) {
        throw "Generated operations key is too short."
    }
    if ($firstOutput.Contains($firstKey)) {
        throw "Generated operations key must not be printed."
    }

    $secondOutput = & $scriptPath -EnvPath $envPath 6>&1 | Out-String
    $secondKey = Get-OperationsKey -Path $envPath
    if ($secondKey -ne $firstKey) {
        throw "Existing operations key was not reused."
    }
    if ($secondOutput.Contains($secondKey)) {
        throw "Reused operations key must not be printed."
    }

    $rotateOutput = & $scriptPath -EnvPath $envPath -Rotate 6>&1 | Out-String
    $rotatedKey = Get-OperationsKey -Path $envPath
    if ($rotatedKey -eq $firstKey) {
        throw "Operations key was not rotated."
    }
    if ($rotateOutput.Contains($firstKey) -or $rotateOutput.Contains($rotatedKey)) {
        throw "Old or rotated operations key must not be printed."
    }

    $lines = [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)
    if (-not $lines.Contains("UNRELATED_SETTING=keep-me")) {
        throw "Unrelated environment configuration was not preserved."
    }
    if (($lines | Where-Object { $_.StartsWith("OPERATIONS_API_KEY=") }).Count -ne 1) {
        throw "Operations key must be stored exactly once."
    }

    Write-Output "PASS: Operations API key generation, reuse, rotation, and secret-safe output are verified."
}
finally {
    $firstKey = $null
    $secondKey = $null
    $rotatedKey = $null
    $resolvedTempRoot = [IO.Path]::GetFullPath($tempRoot)
    if ($resolvedTempRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
