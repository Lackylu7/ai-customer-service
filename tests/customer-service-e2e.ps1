param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [string]$AiServiceUrl = "http://127.0.0.1:5001"
)

$ErrorActionPreference = "Stop"
$knownQuestion = [Text.RegularExpressions.Regex]::Unescape('\u9000\u6b3e\u4e00\u822c\u591a\u4e45\u5230\u8d26\uff1f')
$knownAnswerPattern = [Text.RegularExpressions.Regex]::Unescape('1.{0,3}7\u4e2a\u5de5\u4f5c\u65e5')
$handoffQuestion = [Text.RegularExpressions.Regex]::Unescape('\u6211\u8981\u8f6c\u4eba\u5de5')
$handoffPattern = [Text.RegularExpressions.Regex]::Unescape('\u8f6c\u63a5\u4eba\u5de5')
$afterHandoffQuestion = [Text.RegularExpressions.Regex]::Unescape('\u4eba\u5de5\u63a5\u5165\u540e\u518d\u53d1\u4e00\u6761\u6d88\u606f')

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

function Invoke-ChatwootJson {
    param(
        [ValidateSet("Get", "Post", "Delete")]
        [string]$Method,
        [string]$Path,
        [object]$Body = $null
    )

    $parameters = @{
        Uri = "$chatwootBaseUrl$Path"
        Method = $Method
        Headers = $chatwootHeaders
        TimeoutSec = 30
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json; charset=utf-8"
        $parameters.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress -Depth 10))
    }
    return Invoke-RestMethod @parameters
}

function Invoke-OperationsJson {
    param(
        [ValidateSet("Get", "Patch")]
        [string]$Method,
        [string]$Path,
        [object]$Body = $null
    )

    $parameters = @{
        Uri = "$($AiServiceUrl.TrimEnd('/'))$Path"
        Method = $Method
        Headers = $operationsHeaders
        TimeoutSec = 30
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json; charset=utf-8"
        $parameters.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress -Depth 10))
    }
    return Invoke-RestMethod @parameters
}

function Get-OutgoingMessages {
    param([int]$ConversationId)

    $result = Invoke-ChatwootJson -Method Get -Path "/api/v1/accounts/$accountId/conversations/$ConversationId/messages"
    return @($result.payload | Where-Object { [int]$_.message_type -eq 1 -and -not [bool]$_.private })
}

function Wait-ForOutgoingCount {
    param(
        [int]$ConversationId,
        [int]$ExpectedCount,
        [int]$TimeoutSeconds = 20
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $messages = @(Get-OutgoingMessages -ConversationId $ConversationId)
        if ($messages.Count -eq $ExpectedCount) {
            return $messages
        }
        if ($messages.Count -gt $ExpectedCount) {
            throw "Conversation received more outgoing messages than expected."
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Timed out waiting for outgoing Chatwoot messages."
}

function Send-SignedWebhook {
    param(
        [long]$MessageId,
        [int]$ConversationId,
        [int]$SenderId,
        [string]$Content,
        [string[]]$Labels = @()
    )

    $payload = [ordered]@{
        event = "message_created"
        id = $MessageId
        content = $Content
        message_type = "incoming"
        content_type = "text"
        private = $false
        account = @{ id = $accountId }
        conversation = @{ id = $ConversationId; labels = $Labels }
        sender = @{ id = $SenderId; type = "contact" }
    }
    $body = $payload | ConvertTo-Json -Compress -Depth 8
    $bodyBytes = [Text.Encoding]::UTF8.GetBytes($body)
    $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $signedBytes = [Text.Encoding]::UTF8.GetBytes("$timestamp.$body")
    $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($webhookSecret))
    try {
        $digest = $hmac.ComputeHash($signedBytes)
    }
    finally {
        $hmac.Dispose()
    }
    $hexDigest = ([BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    $signature = "sha256=$hexDigest"

    return Invoke-RestMethod `
        -Uri "$($AiServiceUrl.TrimEnd('/'))/v1/chatwoot/webhook" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Headers @{
            "X-Chatwoot-Timestamp" = $timestamp
            "X-Chatwoot-Signature" = $signature
        } `
        -Body $bodyBytes `
        -TimeoutSec 90
}

$configuration = Read-EnvFile -Path $EnvPath
$required = @(
    "CHATWOOT_BASE_URL",
    "CHATWOOT_ACCOUNT_ID",
    "CHATWOOT_INBOX_ID",
    "CHATWOOT_API_TOKEN",
    "CHATWOOT_WEBHOOK_SECRET",
    "OPERATIONS_API_KEY"
)
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([string]$configuration[$name])) {
        throw "Customer service configuration is incomplete."
    }
}

$chatwootBaseUrl = ([string]$configuration["CHATWOOT_BASE_URL"]).TrimEnd('/')
$accountId = [int]$configuration["CHATWOOT_ACCOUNT_ID"]
$inboxId = [int]$configuration["CHATWOOT_INBOX_ID"]
$apiToken = [string]$configuration["CHATWOOT_API_TOKEN"]
$webhookSecret = [string]$configuration["CHATWOOT_WEBHOOK_SECRET"]
$operationsKey = [string]$configuration["OPERATIONS_API_KEY"]
$chatwootHeaders = @{ api_access_token = $apiToken; Accept = "application/json" }
$operationsHeaders = @{ "X-Operations-Key" = $operationsKey; Accept = "application/json" }
$conversationId = 0
$gapConversationIdOne = 0
$contactId = 0

try {
    $aiHealth = Invoke-RestMethod -Uri "$($AiServiceUrl.TrimEnd('/'))/health" -TimeoutSec 10
    if ($aiHealth.status -ne "ok") { throw "AI service is unhealthy." }
    Invoke-WebRequest -Uri $chatwootBaseUrl -UseBasicParsing -TimeoutSec 10 | Out-Null
    $summaryBefore = Invoke-OperationsJson -Method Get -Path "/v1/operations/summary?window=all"

    $testId = [Guid]::NewGuid().ToString("N")
    $sourceId = "e2e-$testId"
    $contact = Invoke-ChatwootJson -Method Post -Path "/api/v1/accounts/$accountId/contacts" -Body @{
        name = "E2E Customer"
        identifier = $sourceId
        inbox_id = $inboxId
        source_id = $sourceId
    }
    $contactId = [int]$contact.payload.contact.id
    if ($contactId -le 0) { throw "Chatwoot did not create the E2E contact." }

    $conversation = Invoke-ChatwootJson -Method Post -Path "/api/v1/accounts/$accountId/conversations" -Body @{
        inbox_id = $inboxId
        contact_id = $contactId
        source_id = $sourceId
        status = "open"
    }
    $conversationId = [int]$conversation.id
    if ($conversationId -le 0) { throw "Chatwoot did not create the E2E conversation." }

    $messageBase = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $known = Send-SignedWebhook -MessageId $messageBase -ConversationId $conversationId -SenderId $contactId -Content $knownQuestion
    if ($known.status -ne "replied") {
        $unexpectedMessages = @(Wait-ForOutgoingCount -ConversationId $conversationId -ExpectedCount 1)
        throw "Known question returned '$($known.status)': $($unexpectedMessages[-1].content)"
    }
    $knownMessages = @(Wait-ForOutgoingCount -ConversationId $conversationId -ExpectedCount 1)
    if ($knownMessages[0].content -notmatch $knownAnswerPattern) {
        throw "Known answer is not grounded in the expected refund policy."
    }

    $duplicate = Send-SignedWebhook -MessageId $messageBase -ConversationId $conversationId -SenderId $contactId -Content $knownQuestion
    if ($duplicate.status -ne "duplicate") { throw "Duplicate webhook was not ignored." }
    $null = Wait-ForOutgoingCount -ConversationId $conversationId -ExpectedCount 1

    $handoff = Send-SignedWebhook -MessageId ($messageBase + 1) -ConversationId $conversationId -SenderId $contactId -Content $handoffQuestion
    if ($handoff.status -ne "handed_off") { throw "Human-support request did not trigger handoff." }
    $handoffMessages = @(Wait-ForOutgoingCount -ConversationId $conversationId -ExpectedCount 2)
    if ($handoffMessages[-1].content -notmatch $handoffPattern) {
        throw "Customer did not receive the handoff notice."
    }
    $labels = Invoke-ChatwootJson -Method Get -Path "/api/v1/accounts/$accountId/conversations/$conversationId/labels"
    if (-not (@($labels.payload) -contains "ai_handoff")) {
        throw "Chatwoot conversation is missing the ai_handoff label."
    }

    $afterHandoff = Send-SignedWebhook -MessageId ($messageBase + 2) -ConversationId $conversationId -SenderId $contactId -Content $afterHandoffQuestion
    if ($afterHandoff.status -ne "ignored_human_mode") {
        throw "AI did not remain silent after handoff."
    }
    $null = Wait-ForOutgoingCount -ConversationId $conversationId -ExpectedCount 2

    $gapConversationOne = Invoke-ChatwootJson -Method Post -Path "/api/v1/accounts/$accountId/conversations" -Body @{
        inbox_id = $inboxId
        contact_id = $contactId
        source_id = $sourceId
        status = "open"
    }
    $gapConversationIdOne = [int]$gapConversationOne.id
    if ($gapConversationIdOne -le 0) { throw "Chatwoot did not create the first knowledge-gap conversation." }

    $gapQuestionOne = "E2E-$testId`: what is the teleportation octopus policy code?"
    $gapOne = Send-SignedWebhook -MessageId ($messageBase + 3) -ConversationId $gapConversationIdOne -SenderId $contactId -Content $gapQuestionOne
    if ($gapOne.status -ne "handed_off") { throw "First unknown question did not trigger knowledge-gap handoff." }
    $null = Wait-ForOutgoingCount -ConversationId $gapConversationIdOne -ExpectedCount 1

    $summaryAfter = Invoke-OperationsJson -Method Get -Path "/v1/operations/summary?window=all"
    if (($summaryAfter.replied - $summaryBefore.replied) -lt 1) { throw "Operations summary did not record the AI reply." }
    if (($summaryAfter.duplicate_webhooks - $summaryBefore.duplicate_webhooks) -lt 1) { throw "Operations summary did not record the duplicate webhook." }
    if (($summaryAfter.ignored_human_mode - $summaryBefore.ignored_human_mode) -lt 1) { throw "Operations summary did not record human-mode silence." }
    if (($summaryAfter.handoff_reasons.explicit_human_request - $summaryBefore.handoff_reasons.explicit_human_request) -lt 1) { throw "Operations summary did not record explicit handoff." }
    if (($summaryAfter.handoff_reasons.knowledge_gap - $summaryBefore.handoff_reasons.knowledge_gap) -lt 1) { throw "Operations summary did not record the knowledge-gap handoff." }

    $gapList = Invoke-OperationsJson -Method Get -Path "/v1/operations/gaps?status=pending&limit=200&offset=0"
    $matchingGaps = @($gapList.items | Where-Object { $_.sample_question -like "*$testId*" })
    if ($matchingGaps.Count -ne 1) { throw "Unknown question was not added to the knowledge-gap queue." }
    if ([int]$matchingGaps[0].occurrence_count -ne 1) { throw "Knowledge-gap occurrence count is incorrect." }

    $updatedGap = Invoke-OperationsJson -Method Patch -Path "/v1/operations/gaps/$([int]$matchingGaps[0].id)" -Body @{
        status = "resolved"
        note = "E2E verified knowledge maintenance workflow."
    }
    if ($updatedGap.status -ne "resolved" -or $updatedGap.note -ne "E2E verified knowledge maintenance workflow.") {
        throw "Knowledge-gap status or note update was not persisted."
    }

    Write-Output "PASS: Customer-service reply, idempotency, handoff, AI silence, metrics, and knowledge-gap operations are verified."
}
finally {
    foreach ($createdConversationId in @($gapConversationIdOne, $conversationId)) {
        if ($createdConversationId -le 0) { continue }
        try {
            Invoke-ChatwootJson -Method Delete -Path "/api/v1/accounts/$accountId/conversations/$createdConversationId" | Out-Null
        }
        catch {}
    }
    if ($contactId -gt 0) {
        try {
            Invoke-ChatwootJson -Method Delete -Path "/api/v1/accounts/$accountId/contacts/$contactId" | Out-Null
        }
        catch {}
    }
    $apiToken = $null
    $webhookSecret = $null
    $operationsKey = $null
    $chatwootHeaders = $null
    $operationsHeaders = $null
    $configuration.Clear()
}
