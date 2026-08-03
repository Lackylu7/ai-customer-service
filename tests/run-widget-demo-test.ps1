$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot "scripts\run-widget-demo.ps1"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("widget-demo-test-" + [Guid]::NewGuid().ToString("N"))
$configPath = Join-Path $temporaryRoot "enterprise.json"
$envPath = Join-Path $temporaryRoot ".env"
$runtimePath = Join-Path $temporaryRoot "runtime"
$ownedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()

function Get-FreePort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

try {
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    $config = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot "config\enterprise.example.json") | ConvertFrom-Json
    $unicodeCompanyName = "$([char]0x661F)$([char]0x6CB3)$([char]0x4F18)$([char]0x9009) & Shop"
    $config.company.name = $unicodeCompanyName
    $config.assistant.name = 'Test AI Support'
    $config.assistant.welcome_message = 'Welcome to Test Shop.'
    $config.chatwoot.widget_color = '#123456'
    [IO.File]::WriteAllText(
        $configPath,
        ($config | ConvertTo-Json -Depth 8),
        [Text.UTF8Encoding]::new($false)
    )
    $websiteToken = "public-widget-fixture-token"
    [IO.File]::WriteAllLines(
        $envPath,
        @(
            "CHATWOOT_BASE_URL=http://127.0.0.1:3000",
            "CHATWOOT_WEBSITE_TOKEN=$websiteToken"
        ),
        [Text.UTF8Encoding]::new($false)
    )

    $renderOutput = & $scriptPath -ConfigPath $configPath -EnvPath $envPath -RuntimePath $runtimePath -RenderOnly 6>&1 | Out-String
    $renderedPath = Join-Path $runtimePath "index.html"
    $rendered = [IO.File]::ReadAllText($renderedPath, [Text.Encoding]::UTF8)
    foreach ($expected in @(
        '<meta name="ai-customer-service-demo" content="1">',
        ([Net.WebUtility]::HtmlEncode($unicodeCompanyName)),
        'Test AI Support',
        'Welcome to Test Shop.',
        '#123456',
        $websiteToken
    )) {
        if (-not $rendered.Contains($expected)) {
            throw "Widget render did not contain a configured value."
        }
    }
    if ($renderOutput.Contains($websiteToken)) {
        throw "Widget renderer printed the public token."
    }

    $port = Get-FreePort
    [IO.Directory]::CreateDirectory($runtimePath) | Out-Null
    [IO.File]::WriteAllText((Join-Path $runtimePath "server.pid"), "999999", [Text.Encoding]::ASCII)
    $startOutput = & $scriptPath -ConfigPath $configPath -EnvPath $envPath -RuntimePath $runtimePath -Port $port 6>&1 | Out-String
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 5
    if ($response.StatusCode -ne 200 -or $response.Content -notmatch 'ai-customer-service-demo') {
        throw "Widget demo did not start with the project marker."
    }
    $startedPid = [int]([IO.File]::ReadAllText((Join-Path $runtimePath "server.pid")).Trim())
    $startedProcess = Get-Process -Id $startedPid -ErrorAction Stop
    $ownedProcesses.Add($startedProcess)

    $occupiedPort = Get-FreePort
    $occupiedRoot = Join-Path $temporaryRoot "occupied"
    [IO.Directory]::CreateDirectory($occupiedRoot) | Out-Null
    $occupiedProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList "-m http.server $occupiedPort --bind 127.0.0.1 --directory `"$occupiedRoot`"" `
        -WindowStyle Hidden -PassThru
    $ownedProcesses.Add($occupiedProcess)
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 100
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$occupiedPort/" -UseBasicParsing -TimeoutSec 1 | Out-Null
            break
        }
        catch {}
    }
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $occupiedOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -ConfigPath $configPath -EnvPath $envPath -RuntimePath $runtimePath -Port $occupiedPort 2>&1 | Out-String
    $occupiedExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    if ($occupiedExitCode -eq 0 -or $occupiedOutput -notmatch 'occupied by another service') {
        throw "Widget demo did not reject an unrelated service on the target port."
    }
    if ($occupiedProcess.HasExited) {
        throw "Widget demo stopped an unrelated process."
    }

    Write-Output "PASS: Widget rendering, startup, stale PID, and occupied-port behavior are verified."
}
finally {
    foreach ($process in $ownedProcesses) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if ($temporaryRoot.StartsWith([IO.Path]::GetTempPath(), [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
