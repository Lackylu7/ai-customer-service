$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot "scripts\configure-dify-knowledge.ps1"
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("dify-env-test-" + [Guid]::NewGuid().ToString("N"))
$envPath = Join-Path $tempRoot ".env"
$examplePath = Join-Path $tempRoot ".env.example"
$datasetId = "11111111-2222-3333-4444-555555555555"
$fakeApiKey = "dataset-fake-key-for-tests"

New-Item -ItemType Directory -Path $tempRoot | Out-Null

try {
    @(
        "DOCLING_PORT=5001"
        "DIFY_BASE_URL=http://127.0.0.1:8081"
        "DIFY_DATASET_ID="
        "DIFY_DATASET_API_KEY="
        "DIFY_INDEXING_TECHNIQUE=economy"
    ) | Set-Content -LiteralPath $examplePath -Encoding UTF8

    $secureApiKey = ConvertTo-SecureString $fakeApiKey -AsPlainText -Force
    $output = & $scriptPath -DatasetId $datasetId -ApiKey $secureApiKey -EnvPath $envPath 6>&1 | Out-String

    if (-not (Test-Path -LiteralPath $envPath)) {
        throw "Expected the local .env file to be created."
    }

    $content = [IO.File]::ReadAllText($envPath, [Text.Encoding]::UTF8)
    $lines = [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)
    if (-not $lines.Contains("DIFY_DATASET_ID=$datasetId")) {
        throw "Expected the dataset ID to be stored in .env."
    }
    if (-not $lines.Contains("DIFY_DATASET_API_KEY=$fakeApiKey")) {
        throw "Expected the API key to be stored in .env."
    }
    if ($output.Contains($fakeApiKey)) {
        throw "The API key must not be printed to the terminal."
    }

    Write-Host "PASS: configure-dify-knowledge.ps1 writes local settings without printing the API key."
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
