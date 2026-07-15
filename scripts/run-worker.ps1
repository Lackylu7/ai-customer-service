$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "AI customer-service Worker is not installed. Install the project requirements first."
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

Push-Location $projectRoot
try {
    & $pythonExecutable -m app.worker
} finally {
    Pop-Location
}
