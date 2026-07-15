param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [string]$AiServiceUrl = "http://127.0.0.1:5001"
)

$ErrorActionPreference = "Stop"
$question = [Text.RegularExpressions.Regex]::Unescape('\u9000\u6b3e\u4e00\u822c\u591a\u4e45\u5230\u8d26\uff1f')
$platformTimePattern = '2\s*\u4e2a?\u5de5\u4f5c\u65e5'
$paymentTimePattern = '1\s*[\u2013\u2014\-\u81f3\u5230]\s*7\s*\u4e2a?\u5de5\u4f5c\u65e5'

function Read-EnvFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Local .env file does not exist."
    }
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if ($line -match '^\s*([^#=][^=]*)=(.*)$') {
            $values[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    return $values
}

function Invoke-WidgetChatwootJson {
    param(
        [ValidateSet("Post")]
        [string]$Method,
        [string]$Path,
        [object]$Body,
        [hashtable]$Headers = @{ Accept = "application/json" }
    )

    try {
        return Invoke-RestMethod `
            -Uri "$chatwootBaseUrl$Path" `
            -Method $Method `
            -Headers $Headers `
            -ContentType "application/json; charset=utf-8" `
            -Body ([Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress -Depth 8))) `
            -TimeoutSec 30
    }
    catch {
        throw "Chatwoot Widget API request failed."
    }
}

function Invoke-AccountChatwootJson {
    param(
        [ValidateSet("Get", "Delete")]
        [string]$Method,
        [string]$Path
    )

    return Invoke-RestMethod `
        -Uri "$chatwootBaseUrl$Path" `
        -Method $Method `
        -Headers $accountHeaders `
        -TimeoutSec 30
}

function Wait-ForSingleGroundedAnswer {
    param(
        [int]$ConversationDisplayId,
        [int]$TimeoutSeconds = 90
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $result = Invoke-AccountChatwootJson `
            -Method Get `
            -Path "/api/v1/accounts/$accountId/conversations/$ConversationDisplayId/messages"
        $outgoing = @($result.payload | Where-Object {
            (([string]$_.message_type -eq "1") -or ([string]$_.message_type -eq "outgoing")) -and
            -not [bool]$_.private
        })
        if ($outgoing.Count -gt 1) {
            throw "Real Chatwoot webhook produced duplicate public outgoing messages."
        }
        $matching = @($outgoing | Where-Object {
            ([string]$_.content -match $platformTimePattern) -and
            ([string]$_.content -match $paymentTimePattern)
        })
        if ($outgoing.Count -eq 1 -and $matching.Count -eq 1) {
            return
        }
        Start-Sleep -Milliseconds 750
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for the grounded refund answer."
}

$configuration = Read-EnvFile -Path $EnvPath
$required = @(
    "CHATWOOT_BASE_URL",
    "CHATWOOT_WEBSITE_TOKEN",
    "CHATWOOT_ACCOUNT_ID",
    "CHATWOOT_API_TOKEN"
)
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([string]$configuration[$name])) {
        throw "Real Chatwoot webhook configuration is incomplete."
    }
}

$chatwootBaseUrl = ([string]$configuration["CHATWOOT_BASE_URL"]).TrimEnd('/')
$websiteToken = [string]$configuration["CHATWOOT_WEBSITE_TOKEN"]
$accountId = [int]$configuration["CHATWOOT_ACCOUNT_ID"]
$apiToken = [string]$configuration["CHATWOOT_API_TOKEN"]
$accountHeaders = @{ api_access_token = $apiToken; Accept = "application/json" }
$contactId = 0
$conversationDisplayId = 0
$widgetAuthToken = $null
$widgetHeaders = $null

try {
    $ready = Invoke-RestMethod -Uri "$($AiServiceUrl.TrimEnd('/'))/ready" -TimeoutSec 10
    if ($ready.status -ne "ready") {
        throw "AI service Worker is not ready."
    }

    $testId = [Guid]::NewGuid().ToString("N")
    $widgetConfig = Invoke-WidgetChatwootJson `
        -Method Post `
        -Path "/api/v1/widget/config" `
        -Body @{ website_token = $websiteToken }
    $contactId = [int]$widgetConfig.contact.id
    $widgetAuthToken = [string]$widgetConfig.website_channel_config.auth_token
    if ($contactId -le 0 -or [string]::IsNullOrWhiteSpace($widgetAuthToken)) {
        throw "Chatwoot did not create the Widget E2E contact."
    }
    $widgetHeaders = @{ Accept = "application/json"; "X-Auth-Token" = $widgetAuthToken }

    $conversation = Invoke-WidgetChatwootJson `
        -Method Post `
        -Path "/api/v1/widget/conversations" `
        -Headers $widgetHeaders `
        -Body @{
            website_token = $websiteToken
            contact = @{ name = "Real Webhook E2E" }
            message = @{
                content = $question
                echo_id = "e2e-$testId"
                referer_url = "http://localhost/real-webhook-e2e"
                timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            }
        }
    $conversationDisplayId = [int]$conversation.id
    if ($conversationDisplayId -le 0) {
        throw "Chatwoot did not create the Widget E2E conversation."
    }

    Wait-ForSingleGroundedAnswer -ConversationDisplayId $conversationDisplayId
    Write-Output "PASS: A real Chatwoot Widget message triggered exactly one grounded AI answer through Webhook and Worker."
}
finally {
    if ($conversationDisplayId -gt 0) {
        try {
            Invoke-AccountChatwootJson `
                -Method Delete `
                -Path "/api/v1/accounts/$accountId/conversations/$conversationDisplayId" | Out-Null
        }
        catch {}
    }
    if ($contactId -gt 0) {
        try {
            Invoke-AccountChatwootJson `
                -Method Delete `
                -Path "/api/v1/accounts/$accountId/contacts/$contactId" | Out-Null
        }
        catch {}
    }
    $websiteToken = $null
    $apiToken = $null
    $widgetAuthToken = $null
    $widgetHeaders = $null
    $accountHeaders = $null
    $configuration.Clear()
}
