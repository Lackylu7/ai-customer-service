param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, [int]::MaxValue)]
    [int]$MessageId
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "AI customer-service Worker is not installed. Install the project requirements first."
}

if (-not $env:CUSTOMER_SERVICE_DB_PATH -and (Test-Path -LiteralPath $envPath)) {
    foreach ($line in [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)) {
        $trimmedLine = $line.Trim()
        if (-not $trimmedLine -or $trimmedLine.StartsWith("#")) {
            continue
        }
        $separatorIndex = $trimmedLine.IndexOf("=")
        if ($separatorIndex -lt 1) {
            continue
        }
        $name = $trimmedLine.Substring(0, $separatorIndex).Trim()
        if ($name -ne "CUSTOMER_SERVICE_DB_PATH") {
            continue
        }
        $value = $trimmedLine.Substring($separatorIndex + 1).Trim()
        if ($value.Length -ge 2) {
            $isDoubleQuoted = $value.StartsWith('"') -and $value.EndsWith('"')
            $isSingleQuoted = $value.StartsWith("'") -and $value.EndsWith("'")
            if ($isDoubleQuoted -or $isSingleQuoted) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $env:CUSTOMER_SERVICE_DB_PATH = $value
        break
    }
}

$databasePath = if ($env:CUSTOMER_SERVICE_DB_PATH) {
    $env:CUSTOMER_SERVICE_DB_PATH
} else {
    ".runtime/customer-service/state.db"
}

$retryCode = @'
import sys
import time

from app.job_store import SQLiteJobStore

store = SQLiteJobStore(sys.argv[1])
if not store.retry_failed_message(int(sys.argv[2]), now=int(time.time())):
    raise SystemExit('The failed customer-service job was not found.')
'@

Push-Location $projectRoot
try {
    & $pythonExecutable -c $retryCode $databasePath $MessageId
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    Write-Output "PASS: Failed customer-service job was queued for retry."
} finally {
    Pop-Location
}
