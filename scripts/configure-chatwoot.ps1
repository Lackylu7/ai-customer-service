param(
    [string]$WebhookUrl = "http://host.docker.internal:5001/v1/chatwoot/webhook",
    [string]$ConfigPath = "config\enterprise.example.json",
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [string]$FixturePath = "",
    [switch]$RotateWebhookSecret
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configCliPath = Join-Path $projectRoot "app\enterprise_config.py"
$containerName = "chatwoot-local-rails-1"
$operationId = [Guid]::NewGuid().ToString("N")
$rubySource = Join-Path $PSScriptRoot "configure_chatwoot.rb"
$containerRubyPath = "/tmp/configure-chatwoot-$operationId.rb"
$containerSecretPath = "/tmp/chatwoot-config-$operationId.json"
$containerConfigPath = "/tmp/enterprise-config-$operationId.json"
$localSecretPath = Join-Path ([IO.Path]::GetTempPath()) "chatwoot-config-$operationId.json"

function Set-EnvValue {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [string]$Name,
        [string]$Value
    )
    $prefix = "$Name="
    for ($index = 0; $index -lt $Lines.Count; $index++) {
        if ($Lines[$index].StartsWith($prefix, [StringComparison]::Ordinal)) {
            $Lines[$index] = "$prefix$Value"
            return
        }
    }
    $Lines.Add("$prefix$Value")
}

if (-not [IO.Path]::IsPathRooted($ConfigPath)) {
    $resolvedConfigPath = [IO.Path]::GetFullPath((Join-Path $projectRoot $ConfigPath))
}
else {
    $resolvedConfigPath = [IO.Path]::GetFullPath($ConfigPath)
}
if (-not [IO.Path]::IsPathRooted($EnvPath)) {
    $EnvPath = [IO.Path]::GetFullPath((Join-Path $projectRoot $EnvPath))
}
if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw "Local .env file does not exist."
}
if (-not (Test-Path -LiteralPath $resolvedConfigPath)) {
    throw "Enterprise configuration does not exist."
}
$configOutput = & $pythonPath $configCliPath --path $resolvedConfigPath 2>&1
if ($LASTEXITCODE -ne 0) {
    throw (($configOutput | Select-Object -Last 1) -join "")
}

try {
    if (-not [string]::IsNullOrWhiteSpace($FixturePath)) {
        $configuration = Get-Content -Raw -Encoding UTF8 -LiteralPath $FixturePath | ConvertFrom-Json
    }
    else {
        $health = Invoke-WebRequest -Uri "http://127.0.0.1:3000/" -UseBasicParsing -TimeoutSec 15
        if ($health.StatusCode -ne 200) { throw "Chatwoot is not healthy." }
        if (-not (Test-Path -LiteralPath $rubySource)) { throw "Chatwoot provisioner is missing." }

        docker cp $rubySource "${containerName}:$containerRubyPath" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to copy the Chatwoot provisioner." }
        docker cp $resolvedConfigPath "${containerName}:$containerConfigPath" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to copy the enterprise configuration." }

        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $rotateSecretValue = if ($RotateWebhookSecret.IsPresent) { "1" } else { "0" }
        $provisionOutput = docker exec -e "ROTATE_CHATWOOT_WEBHOOK_SECRET=$rotateSecretValue" --workdir /app $containerName bundle exec rails runner $containerRubyPath $WebhookUrl $containerSecretPath $containerConfigPath 2>&1
        $provisionExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        if ($provisionExitCode -ne 0) {
            $diagnostic = (($provisionOutput | Select-Object -Last 20) -join [Environment]::NewLine)
            $diagnostic = $diagnostic -replace '[A-Za-z0-9_-]{32,}', '[REDACTED]'
            throw "Chatwoot provisioning failed. $diagnostic"
        }

        docker cp "${containerName}:$containerSecretPath" $localSecretPath | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to transfer Chatwoot settings." }
        $configuration = Get-Content -Raw -Encoding UTF8 -LiteralPath $localSecretPath | ConvertFrom-Json
    }

    if (
        [int]$configuration.account_id -le 0 -or
        [int]$configuration.inbox_id -le 0 -or
        [string]::IsNullOrWhiteSpace([string]$configuration.api_token) -or
        [string]::IsNullOrWhiteSpace([string]$configuration.webhook_secret) -or
        [string]::IsNullOrWhiteSpace([string]$configuration.website_token)
    ) {
        throw "Chatwoot returned incomplete integration settings."
    }

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.AddRange([IO.File]::ReadAllLines($EnvPath, [Text.Encoding]::UTF8))
    Set-EnvValue -Lines $lines -Name "CHATWOOT_BASE_URL" -Value "http://127.0.0.1:3000"
    Set-EnvValue -Lines $lines -Name "CHATWOOT_ACCOUNT_ID" -Value ([string]$configuration.account_id)
    Set-EnvValue -Lines $lines -Name "CHATWOOT_INBOX_ID" -Value ([string]$configuration.inbox_id)
    Set-EnvValue -Lines $lines -Name "CHATWOOT_API_TOKEN" -Value ([string]$configuration.api_token)
    Set-EnvValue -Lines $lines -Name "CHATWOOT_WEBHOOK_SECRET" -Value ([string]$configuration.webhook_secret)
    Set-EnvValue -Lines $lines -Name "CHATWOOT_WEBSITE_TOKEN" -Value ([string]$configuration.website_token)
    Set-EnvValue -Lines $lines -Name "ENTERPRISE_CONFIG_PATH" -Value $ConfigPath
    [IO.File]::WriteAllLines($EnvPath, $lines, [Text.UTF8Encoding]::new($false))

    Write-Output "PASS: Chatwoot Website Inbox and signed webhook are configured."
}
finally {
    $configuration = $null
    Remove-Item -LiteralPath $localSecretPath -Force -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($FixturePath)) {
        docker exec --user root $containerName sh -c "rm -f '$containerRubyPath' '$containerSecretPath' '$containerConfigPath'" 2>$null | Out-Null
    }
}
