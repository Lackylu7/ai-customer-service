$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot "scripts\provision-dify-chatflow.ps1"
$configPath = Join-Path $projectRoot "config\enterprise.example.json"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("dify-provision-test-" + [Guid]::NewGuid().ToString("N"))
$envPath = Join-Path $temporaryRoot ".env"
$fixturePath = Join-Path $temporaryRoot "fixture.json"

try {
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    [IO.File]::WriteAllLines(
        $envPath,
        @(
            "UNRELATED_VALUE=keep-me",
            "DIFY_DATASET_ID=",
            "DIFY_DATASET_API_KEY=",
            "DIFY_INDEXING_TECHNIQUE=",
            "DIFY_APP_ID=",
            "DIFY_APP_API_KEY="
        ),
        [Text.UTF8Encoding]::new($false)
    )
    $fixture = @{
        dataset_id = "11111111-2222-3333-4444-555555555555"
        dataset_api_key = "dataset-fixture-secret"
        app_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        app_api_key = "app-fixture-secret"
    }
    [IO.File]::WriteAllText(
        $fixturePath,
        ($fixture | ConvertTo-Json),
        [Text.UTF8Encoding]::new($false)
    )

    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -ConfigPath $configPath `
        -EnvPath $envPath `
        -FixturePath $fixturePath 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Dify provisioning fixture failed. $output"
    }

    $contents = [IO.File]::ReadAllText($envPath, [Text.Encoding]::UTF8)
    $actualLines = [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)
    $expectedLines = @(
        "DIFY_DATASET_ID=11111111-2222-3333-4444-555555555555",
        "DIFY_DATASET_API_KEY=dataset-fixture-secret",
        "DIFY_INDEXING_TECHNIQUE=economy",
        "DIFY_APP_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "DIFY_APP_API_KEY=app-fixture-secret",
        "UNRELATED_VALUE=keep-me"
    )
    foreach ($line in $expectedLines) {
        if ($actualLines -notcontains $line) {
            throw "Dify provisioning did not write the expected environment value."
        }
    }
    $outputText = [string]$output
    foreach ($secret in @("dataset-fixture-secret", "app-fixture-secret")) {
        if ($outputText.Contains($secret)) {
            throw "Dify provisioning printed a secret."
        }
    }
    Write-Output "PASS: Dify enterprise provisioning stores all identifiers without printing secrets."
}
finally {
    if ($temporaryRoot.StartsWith([IO.Path]::GetTempPath(), [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
