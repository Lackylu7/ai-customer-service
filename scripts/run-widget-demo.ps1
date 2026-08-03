param(
    [string]$ConfigPath = "config\enterprise.example.json",
    [string]$EnvPath = ".env",
    [string]$RuntimePath = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$RenderOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configCliPath = Join-Path $projectRoot "app\enterprise_config.py"
$templatePath = Join-Path $projectRoot "samples\ecommerce\widget-demo.html"

function Resolve-ProjectPath {
    param([string]$Path)
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $Path))
}

function Read-EnvValues {
    param([string]$Path)
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if ($line -match '^\s*([^#=][^=]*)=(.*)$') {
            $values[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    return $values
}

$resolvedConfigPath = Resolve-ProjectPath -Path $ConfigPath
$resolvedEnvPath = Resolve-ProjectPath -Path $EnvPath
if ([string]::IsNullOrWhiteSpace($RuntimePath)) {
    $resolvedRuntimePath = Join-Path $projectRoot ".runtime\customer-service\widget-demo"
}
else {
    $resolvedRuntimePath = Resolve-ProjectPath -Path $RuntimePath
}
$renderedPath = Join-Path $resolvedRuntimePath "index.html"
$pidPath = Join-Path $resolvedRuntimePath "server.pid"

foreach ($requiredPath in @($pythonPath, $configCliPath, $templatePath, $resolvedConfigPath, $resolvedEnvPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Widget demo prerequisite is missing."
    }
}

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$configOutput = & $pythonPath $configCliPath --path $resolvedConfigPath --print-json 2>&1
$configExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($configExitCode -ne 0) {
    throw (($configOutput | Select-Object -Last 1) -join "")
}
$enterprise = ($configOutput -join [Environment]::NewLine) | ConvertFrom-Json
$envValues = Read-EnvValues -Path $resolvedEnvPath
$websiteToken = [string]$envValues["CHATWOOT_WEBSITE_TOKEN"]
$chatwootBaseUrl = ([string]$envValues["CHATWOOT_BASE_URL"]).TrimEnd('/')
if ([string]::IsNullOrWhiteSpace($websiteToken) -or [string]::IsNullOrWhiteSpace($chatwootBaseUrl)) {
    throw "Chatwoot Widget configuration is incomplete."
}

[IO.Directory]::CreateDirectory($resolvedRuntimePath) | Out-Null
$template = [IO.File]::ReadAllText($templatePath, [Text.Encoding]::UTF8)
$rendered = $template
$rendered = $rendered.Replace("__COMPANY_NAME_HTML__", [Net.WebUtility]::HtmlEncode([string]$enterprise.company.name))
$rendered = $rendered.Replace("__ASSISTANT_NAME_HTML__", [Net.WebUtility]::HtmlEncode([string]$enterprise.assistant.name))
$rendered = $rendered.Replace("__WELCOME_MESSAGE_HTML__", [Net.WebUtility]::HtmlEncode([string]$enterprise.assistant.welcome_message))
$rendered = $rendered.Replace("__WIDGET_COLOR_CSS__", [string]$enterprise.chatwoot.widget_color)
$rendered = $rendered.Replace("__CHATWOOT_BASE_URL_JSON__", (ConvertTo-Json -InputObject $chatwootBaseUrl -Compress))
$rendered = $rendered.Replace("__CHATWOOT_WEBSITE_TOKEN_JSON__", (ConvertTo-Json -InputObject $websiteToken -Compress))
if ($rendered -match '__[A-Z0-9_]+__') {
    throw "Widget demo rendering left an unresolved placeholder."
}
[IO.File]::WriteAllText($renderedPath, $rendered, [Text.UTF8Encoding]::new($false))
$websiteToken = $null
$envValues.Clear()

if ($RenderOnly.IsPresent) {
    Write-Output "PASS: Widget demo is rendered."
    exit 0
}

$widgetUrl = "http://127.0.0.1:$Port/"
$existingResponse = $null
try {
    $existingResponse = Invoke-WebRequest -Uri $widgetUrl -UseBasicParsing -TimeoutSec 2
}
catch {}
if ($null -ne $existingResponse) {
    if ($existingResponse.StatusCode -eq 200 -and $existingResponse.Content -match 'ai-customer-service-demo') {
        Write-Output "PASS: Widget demo is already running at $widgetUrl"
        exit 0
    }
    throw "Port $Port is occupied by another service."
}

$process = Start-Process -FilePath $pythonPath `
    -ArgumentList "-m http.server $Port --bind 127.0.0.1 --directory `"$resolvedRuntimePath`"" `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $resolvedRuntimePath "server.stdout.log") `
    -RedirectStandardError (Join-Path $resolvedRuntimePath "server.stderr.log") `
    -PassThru
[IO.File]::WriteAllText($pidPath, [string]$process.Id, [Text.Encoding]::ASCII)

try {
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        if ($process.HasExited) { break }
        try {
            $response = Invoke-WebRequest -Uri $widgetUrl -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200 -and $response.Content -match 'ai-customer-service-demo') {
                Write-Output "PASS: Widget demo is running at $widgetUrl"
                exit 0
            }
        }
        catch {}
    }
    throw "Widget demo did not become ready."
}
catch {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    throw
}
