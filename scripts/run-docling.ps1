$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Docling is not installed. Run '.\.venv\Scripts\python -m pip install -r requirements-docling.txt' first."
}

if (Test-Path -LiteralPath $envPath) {
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
        $value = $trimmedLine.Substring($separatorIndex + 1).Trim()
        if ($value.Length -ge 2) {
            $isDoubleQuoted = $value.StartsWith('"') -and $value.EndsWith('"')
            $isSingleQuoted = $value.StartsWith("'") -and $value.EndsWith("'")
            if ($isDoubleQuoted -or $isSingleQuoted) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }

        if (-not (Test-Path "Env:$name")) {
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

if (-not $env:UVICORN_HOST) {
    $env:UVICORN_HOST = "127.0.0.1"
}
if (-not $env:UVICORN_PORT) {
    $env:UVICORN_PORT = "5001"
}
Push-Location $projectRoot
try {
    & $pythonExecutable -m uvicorn app.main:app `
        --host $env:UVICORN_HOST `
        --port $env:UVICORN_PORT `
        --no-access-log
} finally {
    Pop-Location
}
