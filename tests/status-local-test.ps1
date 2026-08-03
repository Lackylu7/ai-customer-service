$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot "scripts\status-local.ps1"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("status-local-test-" + [Guid]::NewGuid().ToString("N"))
$server = $null

function Get-FreePort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

try {
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    $port = Get-FreePort
    $server = Start-Process -FilePath $pythonPath `
        -ArgumentList "-m http.server $port --bind 127.0.0.1 --directory `"$temporaryRoot`"" `
        -WindowStyle Hidden -PassThru
    $url = "http://127.0.0.1:$port/"
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 100
        try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 1 | Out-Null; break } catch {}
    }

    $onlineOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -ChatwootUrl $url -DifyUrl $url -AiUrl $url -WidgetUrl $url 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or @($onlineOutput -split "`n" | Where-Object { $_ -match 'ONLINE' }).Count -lt 4) {
        throw "Local status did not report all services online."
    }

    $offlinePort = Get-FreePort
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $offlineOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -ChatwootUrl "http://127.0.0.1:$offlinePort/" -DifyUrl $url -AiUrl $url -WidgetUrl $url 2>&1 | Out-String
    $offlineExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    if ($offlineExitCode -eq 0 -or $offlineOutput -notmatch 'Chatwoot\s+OFFLINE') {
        throw "Local status did not report an offline service."
    }

    Write-Output "PASS: Local service status reports online and offline states."
}
finally {
    if ($null -ne $server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
    if ($temporaryRoot.StartsWith([IO.Path]::GetTempPath(), [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
