$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot ".env"
$templatePath = Join-Path $projectRoot "samples\ecommerce\widget-demo.html"
$runtimePath = Join-Path $projectRoot ".runtime\customer-service\widget-demo"
$renderedPath = Join-Path $runtimePath "index.html"
$pidPath = Join-Path $runtimePath "server.pid"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $envPath)) { throw "Local .env file does not exist." }
if (-not (Test-Path -LiteralPath $templatePath)) { throw "Widget demo template does not exist." }

$websiteToken = ""
foreach ($line in [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)) {
    if ($line -match '^CHATWOOT_WEBSITE_TOKEN=(.*)$') {
        $websiteToken = $matches[1].Trim()
        break
    }
}
if ([string]::IsNullOrWhiteSpace($websiteToken)) {
    throw "CHATWOOT_WEBSITE_TOKEN is not configured."
}

New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$template = [IO.File]::ReadAllText($templatePath, [Text.Encoding]::UTF8)
$rendered = $template.Replace("__CHATWOOT_WEBSITE_TOKEN__", $websiteToken)
[IO.File]::WriteAllText($renderedPath, $rendered, [Text.UTF8Encoding]::new($false))
$websiteToken = $null

if (Test-Path -LiteralPath $pidPath) {
    $existingPid = [int]([IO.File]::ReadAllText($pidPath).Trim())
    if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
        Write-Output "PASS: Widget demo is already running at http://127.0.0.1:8000/."
        exit 0
    }
}

$process = Start-Process -FilePath $pythonPath `
    -ArgumentList "-m http.server 8000 --bind 127.0.0.1 --directory `"$runtimePath`"" `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $runtimePath "server.stdout.log") `
    -RedirectStandardError (Join-Path $runtimePath "server.stderr.log") `
    -PassThru
[IO.File]::WriteAllText($pidPath, [string]$process.Id, [Text.Encoding]::ASCII)

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Write-Output "PASS: Widget demo is running at http://127.0.0.1:8000/."
            exit 0
        }
    }
    catch {
        continue
    }
}

throw "Widget demo did not become ready."
