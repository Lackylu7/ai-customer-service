param(
    [Parameter(Mandatory = $true)]
    [int]$ConversationId,
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env")
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

if ($ConversationId -le 0) { throw "ConversationId must be positive." }
if (-not (Test-Path -LiteralPath $EnvPath)) { throw "Local .env file does not exist." }

$config = @{}
foreach ($line in [IO.File]::ReadAllLines($EnvPath, [Text.Encoding]::UTF8)) {
    if ($line -match '^\s*([^#=][^=]*)=(.*)$') {
        $config[$matches[1].Trim()] = $matches[2].Trim()
    }
}

$baseUrl = $config["CHATWOOT_BASE_URL"]
$accountId = $config["CHATWOOT_ACCOUNT_ID"]
$apiToken = $config["CHATWOOT_API_TOKEN"]
$databasePath = $config["CUSTOMER_SERVICE_DB_PATH"]
if ([string]::IsNullOrWhiteSpace($databasePath)) {
    $databasePath = ".runtime/customer-service/state.db"
}
if (
    [string]::IsNullOrWhiteSpace($baseUrl) -or
    [string]::IsNullOrWhiteSpace($accountId) -or
    [string]::IsNullOrWhiteSpace($apiToken)
) {
    throw "Chatwoot configuration is incomplete."
}

try {
    $labelsUrl = "$($baseUrl.TrimEnd('/'))/api/v1/accounts/$accountId/conversations/$ConversationId/labels"
    $headers = @{ api_access_token = $apiToken }
    $current = Invoke-RestMethod -Uri $labelsUrl -Method Get -Headers $headers -TimeoutSec 15
    $remaining = @($current.payload | Where-Object { $_ -ne "ai_handoff" })
    $payload = @{ labels = $remaining } | ConvertTo-Json -Compress
    Invoke-RestMethod -Uri $labelsUrl -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -TimeoutSec 15 | Out-Null

    $python = @'
import sys
from app.conversation_store import SQLiteConversationStore

store = SQLiteConversationStore(sys.argv[1])
store.save_conversation(int(sys.argv[2]), dify_conversation_id=None, mode="ai")
'@
    Push-Location $projectRoot
    try {
        $python | & ".\.venv\Scripts\python.exe" - $databasePath $ConversationId
        if ($LASTEXITCODE -ne 0) { throw "Failed to reset local conversation state." }
    }
    finally {
        Pop-Location
    }
    Write-Output "PASS: Conversation is back in AI mode."
}
finally {
    $apiToken = $null
    $headers = $null
    $config.Clear()
}
