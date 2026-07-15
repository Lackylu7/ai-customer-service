$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot "scripts\configure-chatwoot.ps1"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempRoot = Join-Path $tempBase ("chatwoot-env-test-" + [Guid]::NewGuid().ToString("N"))
$envPath = Join-Path $tempRoot ".env"
$fixturePath = Join-Path $tempRoot "fixture.json"
$fakeApiToken = "fake-chatwoot-token-for-tests"
$fakeWebhookSecret = "fake-webhook-secret-for-tests"

New-Item -ItemType Directory -Path $tempRoot | Out-Null

try {
    @(
        "DIFY_BASE_URL=http://127.0.0.1:8081"
        "UNRELATED_SETTING=keep-me"
        "CHATWOOT_API_TOKEN=old-value"
    ) | Set-Content -LiteralPath $envPath -Encoding UTF8

    @{
        account_id = 7
        api_token = $fakeApiToken
        webhook_secret = $fakeWebhookSecret
        website_token = "public-website-token"
    } | ConvertTo-Json | Set-Content -LiteralPath $fixturePath -Encoding UTF8

    $output = & $scriptPath -EnvPath $envPath -FixturePath $fixturePath -RotateWebhookSecret 6>&1 | Out-String
    $lines = [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)

    foreach ($expected in @(
        "UNRELATED_SETTING=keep-me"
        "CHATWOOT_BASE_URL=http://127.0.0.1:3000"
        "CHATWOOT_ACCOUNT_ID=7"
        "CHATWOOT_API_TOKEN=$fakeApiToken"
        "CHATWOOT_WEBHOOK_SECRET=$fakeWebhookSecret"
        "CHATWOOT_WEBSITE_TOKEN=public-website-token"
    )) {
        if (-not $lines.Contains($expected)) {
            throw "Expected local Chatwoot configuration was not written."
        }
    }
    if ($output.Contains($fakeApiToken) -or $output.Contains($fakeWebhookSecret)) {
        throw "Chatwoot secrets must not be printed."
    }

    Write-Output "PASS: Chatwoot settings are stored without printing secrets."
}
finally {
    $resolvedTempRoot = [IO.Path]::GetFullPath($tempRoot)
    if ($resolvedTempRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
