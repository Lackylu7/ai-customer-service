param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [string]$CasesPath = (Join-Path (Split-Path -Parent $PSScriptRoot) "samples\ecommerce\rag-evaluation.jsonl")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $EnvPath)) { throw "Local .env file does not exist." }
if (-not (Test-Path -LiteralPath $CasesPath)) { throw "RAG evaluation cases do not exist." }

$configuration = @{}
foreach ($line in [IO.File]::ReadAllLines($EnvPath, [Text.Encoding]::UTF8)) {
    if ($line -match '^\s*([^#=][^=]*)=(.*)$') {
        $configuration[$matches[1].Trim()] = $matches[2].Trim()
    }
}

$baseUrl = ([string]$configuration["DIFY_BASE_URL"]).TrimEnd('/')
$datasetId = [string]$configuration["DIFY_DATASET_ID"]
$apiKey = [string]$configuration["DIFY_DATASET_API_KEY"]
if (
    [string]::IsNullOrWhiteSpace($baseUrl) -or
    [string]::IsNullOrWhiteSpace($datasetId) -or
    [string]::IsNullOrWhiteSpace($apiKey)
) {
    throw "Dify knowledge configuration is incomplete."
}

$headers = @{ Authorization = "Bearer $apiKey"; Accept = "application/json" }
$cases = @(
    [IO.File]::ReadAllLines($CasesPath, [Text.Encoding]::UTF8) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { $_ | ConvertFrom-Json }
)
$passed = 0

try {
    foreach ($case in $cases) {
        $payload = @{
            query = [string]$case.question
            retrieval_model = @{
                search_method = "keyword_search"
                reranking_enable = $false
                top_k = 3
                score_threshold_enabled = $false
            }
        } | ConvertTo-Json -Compress -Depth 8

        $response = Invoke-RestMethod `
            -Uri "$baseUrl/v1/datasets/$datasetId/retrieve" `
            -Method Post `
            -Headers $headers `
            -ContentType "application/json; charset=utf-8" `
            -Body ([Text.Encoding]::UTF8.GetBytes($payload)) `
            -TimeoutSec 60

        $retrievedText = (@($response.records | ForEach-Object { [string]$_.segment.content }) -join "`n")
        $missing = @($case.expected_keywords | Where-Object { -not $retrievedText.Contains([string]$_) })
        if ($missing.Count -gt 0) {
            throw "RAG case '$($case.id)' missed $($missing.Count) expected keyword(s)."
        }
        $passed++
    }

    if ($passed -ne $cases.Count) { throw "RAG evaluation did not run every case." }
    Write-Output "PASS: RAG evaluation $passed/$($cases.Count)."
}
finally {
    $apiKey = $null
    $headers = $null
    $configuration.Clear()
}
