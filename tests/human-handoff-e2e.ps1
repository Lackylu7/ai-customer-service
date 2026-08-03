param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [string]$AiServiceUrl = "http://127.0.0.1:5001",
    [string]$PythonPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [string]$CustomerServiceDbPath = "",
    [int]$MessageTimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$handoffQuestion = [Text.RegularExpressions.Regex]::Unescape('\u6211\u8981\u8f6c\u4eba\u5de5')
$faqQuestion = [Text.RegularExpressions.Regex]::Unescape('\u9000\u6b3e\u4e00\u822c\u591a\u4e45\u5230\u8d26\uff1f')
$highRiskQuestion = [Text.RegularExpressions.Regex]::Unescape('\u6211\u7684\u652f\u4ed8\u8d26\u6237\u88ab\u76d7')
$humanReply = [Text.RegularExpressions.Regex]::Unescape('\u4eba\u5de5\u5ba2\u670d\u5df2\u6536\u5230\uff0c\u6b63\u5728\u6838\u5b9e\u3002')
$temporaryAiText = [Text.RegularExpressions.Regex]::Unescape('AI \u4e34\u65f6\u534f\u52a9')
$humanServiceText = [Text.RegularExpressions.Regex]::Unescape('\u4eba\u5de5\u5ba2\u670d')
$businessConclusionPattern = [Text.RegularExpressions.Regex]::Unescape(
    '\u5df2\u7ecf\u5904\u7406|\u5df2\u5904\u7406|\u9000\u6b3e.{0,8}\u5de5\u4f5c\u65e5|48.{0,3}\u5c0f\u65f6.{0,3}\u53d1\u8d27'
)

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

function Get-HttpStatus {
    param([object]$ErrorRecord)

    try {
        $status = $ErrorRecord.Exception.Response.StatusCode
        if ($null -ne $status.value__) {
            return [string]$status.value__
        }
        if ($null -ne $status) {
            return [string][int]$status
        }
    }
    catch {}
    return "network"
}

function Invoke-SafeRestMethod {
    param(
        [hashtable]$Parameters,
        [string]$Step
    )

    try {
        return Invoke-RestMethod @Parameters
    }
    catch {
        $status = Get-HttpStatus -ErrorRecord $_
        throw "$Step failed (HTTP $status)."
    }
}

function Invoke-AccountChatwootJson {
    param(
        [ValidateSet("Get", "Post", "Delete")]
        [string]$Method,
        [string]$Path,
        [object]$Body = $null,
        [string]$Step = "Chatwoot account request"
    )

    $parameters = @{
        Uri = "$chatwootBaseUrl$Path"
        Method = $Method
        Headers = $accountHeaders
        TimeoutSec = 30
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json; charset=utf-8"
        $parameters.Body = [Text.Encoding]::UTF8.GetBytes(
            ($Body | ConvertTo-Json -Compress -Depth 10)
        )
    }
    return Invoke-SafeRestMethod -Parameters $parameters -Step $Step
}

function Invoke-WidgetChatwootJson {
    param(
        [string]$Path,
        [object]$Body,
        [hashtable]$Headers,
        [string]$Step
    )

    $parameters = @{
        Uri = "$chatwootBaseUrl$Path"
        Method = "Post"
        Headers = $Headers
        ContentType = "application/json; charset=utf-8"
        Body = [Text.Encoding]::UTF8.GetBytes(
            ($Body | ConvertTo-Json -Compress -Depth 10)
        )
        TimeoutSec = 30
    }
    return Invoke-SafeRestMethod -Parameters $parameters -Step $Step
}

function Invoke-OperationsJson {
    param(
        [ValidateSet("Get", "Post")]
        [string]$Method,
        [string]$Path,
        [string]$Step
    )

    $parameters = @{
        Uri = "$($AiServiceUrl.TrimEnd('/'))$Path"
        Method = $Method
        Headers = $operationsHeaders
        TimeoutSec = 30
    }
    return Invoke-SafeRestMethod -Parameters $parameters -Step $Step
}

function Get-ConversationMessages {
    param([int]$ConversationId)

    $result = Invoke-AccountChatwootJson `
        -Method Get `
        -Path "/api/v1/accounts/$accountId/conversations/$ConversationId/messages" `
        -Step "Read conversation messages"
    return @($result.payload)
}

function Get-ConversationLabels {
    param([int]$ConversationId)

    $result = Invoke-AccountChatwootJson `
        -Method Get `
        -Path "/api/v1/accounts/$accountId/conversations/$ConversationId/labels" `
        -Step "Read conversation labels"
    return @($result.payload)
}

function Wait-ForLabel {
    param(
        [int]$ConversationId,
        [string]$Label,
        [int]$TimeoutSeconds = 60
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (@(Get-ConversationLabels -ConversationId $ConversationId) -contains $Label) {
            return
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for the handoff label."
}

function Wait-ForSourceMessage {
    param(
        [int]$ConversationId,
        [string]$SourceId,
        [int]$TimeoutSeconds = 90
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $matching = @(Get-ConversationMessages -ConversationId $ConversationId | Where-Object {
            [string]$_.source_id -eq $SourceId
        })
        if ($matching.Count -gt 1) {
            throw "A customer message produced duplicate AI output."
        }
        if ($matching.Count -eq 1) {
            return $matching[0]
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    $observedSources = @(
        Get-ConversationMessages -ConversationId $ConversationId | Where-Object {
            [string]$_.source_id -like "ai-cs:*"
        } | ForEach-Object { [string]$_.source_id }
    )
    $observed = if ($observedSources.Count -eq 0) {
        "none"
    }
    else {
        $observedSources -join ","
    }
    throw "Timed out waiting for $SourceId; observed AI sources: $observed."
}

function Assert-NoSourceMessage {
    param(
        [int]$ConversationId,
        [string]$SourceId,
        [int]$WaitSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
    do {
        $matching = @(Get-ConversationMessages -ConversationId $ConversationId | Where-Object {
            [string]$_.source_id -eq $SourceId
        })
        if ($matching.Count -gt 0) {
            throw "AI fallback was sent after a timely human reply."
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
}

function Wait-ForAiResume {
    param(
        [int]$ConversationId,
        [int]$TimeoutSeconds = 15
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            return Invoke-OperationsJson `
                -Method Post `
                -Path "/v1/operations/conversations/$ConversationId/resume-ai" `
                -Step "Resume AI ownership"
        }
        catch {
            if ([string]$_.Exception.Message -notmatch 'HTTP 409') {
                throw
            }
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for the handoff job to become resumable."
}

function Send-WidgetMessage {
    param(
        [string]$Content,
        [string]$EchoId
    )

    $result = Invoke-WidgetChatwootJson `
        -Path "/api/v1/widget/messages" `
        -Headers $widgetHeaders `
        -Step "Send Widget message" `
        -Body @{
            website_token = $websiteToken
            message = @{
                content = $Content
                echo_id = $EchoId
                referer_url = "http://localhost/human-handoff-e2e"
                timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            }
        }
    $messageId = [int]$result.id
    if ($messageId -le 0) {
        throw "Chatwoot did not return a Widget message ID."
    }
    return $messageId
}

function Stop-ShortWorker {
    param([Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }
    try {
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction Stop
            [void]$Process.WaitForExit(10000)
        }
    }
    catch {
        Write-Warning "The short-SLA Worker required forced cleanup."
    }
}

$configuration = Read-EnvFile -Path $EnvPath
$required = @(
    "DIFY_BASE_URL",
    "DIFY_APP_API_KEY",
    "CHATWOOT_BASE_URL",
    "CHATWOOT_WEBSITE_TOKEN",
    "CHATWOOT_ACCOUNT_ID",
    "CHATWOOT_API_TOKEN",
    "CHATWOOT_WEBHOOK_SECRET",
    "OPERATIONS_API_KEY"
)
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([string]$configuration[$name])) {
        throw "Human handoff E2E configuration is incomplete."
    }
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "The project Python environment does not exist."
}
if ($MessageTimeoutSeconds -lt 5 -or $MessageTimeoutSeconds -gt 300) {
    throw "MessageTimeoutSeconds must be between 5 and 300."
}

$envDirectory = Split-Path -Parent ([IO.Path]::GetFullPath($EnvPath))
$databaseValue = $CustomerServiceDbPath
if ([string]::IsNullOrWhiteSpace($databaseValue)) {
    $databaseValue = [string]$configuration["CUSTOMER_SERVICE_DB_PATH"]
    if ([string]::IsNullOrWhiteSpace($databaseValue)) {
        $databaseValue = ".runtime/customer-service/state.db"
    }
}
if ([IO.Path]::IsPathRooted($databaseValue)) {
    $databasePath = [IO.Path]::GetFullPath($databaseValue)
}
else {
    $databasePath = [IO.Path]::GetFullPath((Join-Path $envDirectory $databaseValue))
}

$chatwootBaseUrl = ([string]$configuration["CHATWOOT_BASE_URL"]).TrimEnd('/')
$websiteToken = [string]$configuration["CHATWOOT_WEBSITE_TOKEN"]
$accountId = [int]$configuration["CHATWOOT_ACCOUNT_ID"]
$apiToken = [string]$configuration["CHATWOOT_API_TOKEN"]
$operationsKey = [string]$configuration["OPERATIONS_API_KEY"]
$accountHeaders = @{ api_access_token = $apiToken; Accept = "application/json" }
$operationsHeaders = @{ "X-Operations-Key" = $operationsKey; Accept = "application/json" }
$workerProcess = $null
$composeWorkerWasRunning = $false
$contactId = 0
$conversationId = 0
$testLabelId = 0
$widgetHeaders = $null
$savedEnvironment = @{}
$workerStdout = Join-Path $projectRoot ".runtime\human-handoff-e2e-worker.stdout.log"
$workerStderr = Join-Path $projectRoot ".runtime\human-handoff-e2e-worker.stderr.log"

try {
    $health = Invoke-SafeRestMethod `
        -Step "Check AI service health" `
        -Parameters @{ Uri = "$($AiServiceUrl.TrimEnd('/'))/health"; TimeoutSec = 10 }
    if ([string]$health.status -ne "ok") {
        throw "AI service is unhealthy."
    }
    Invoke-OperationsJson `
        -Method Get `
        -Path "/v1/operations/human-conversations" `
        -Step "Check handoff operations API" | Out-Null

    Push-Location $envDirectory
    try {
        $runningWorker = @(& docker compose ps --status running -q worker 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw "Docker Compose is unavailable for Worker isolation."
        }
        if ($runningWorker.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($runningWorker[0])) {
            $previousErrorAction = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                & docker compose stop worker *> $null
                $stopExitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previousErrorAction
            }
            if ($stopExitCode -ne 0) {
                throw "The regular Worker could not be stopped."
            }
            $composeWorkerWasRunning = $true
        }
    }
    finally {
        Pop-Location
    }

    $workerEnvironment = @{
        DIFY_BASE_URL = [string]$configuration["DIFY_BASE_URL"]
        DIFY_APP_API_KEY = [string]$configuration["DIFY_APP_API_KEY"]
        CHATWOOT_BASE_URL = [string]$configuration["CHATWOOT_BASE_URL"]
        CHATWOOT_ACCOUNT_ID = [string]$configuration["CHATWOOT_ACCOUNT_ID"]
        CHATWOOT_API_TOKEN = [string]$configuration["CHATWOOT_API_TOKEN"]
        CHATWOOT_WEBHOOK_SECRET = [string]$configuration["CHATWOOT_WEBHOOK_SECRET"]
        CUSTOMER_SERVICE_DB_PATH = $databasePath
        CUSTOMER_SERVICE_HUMAN_SLA_SECONDS = "3"
        CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS = "3"
        CUSTOMER_SERVICE_RECONCILE_INTERVAL_SECONDS = "60"
    }
    foreach ($entry in $workerEnvironment.GetEnumerator()) {
        $savedEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable(
            $entry.Key,
            [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            $entry.Key,
            [string]$entry.Value,
            [EnvironmentVariableTarget]::Process
        )
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $workerStdout) -Force | Out-Null
    $workerProcess = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @("-m", "app.worker") `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $workerStdout `
        -RedirectStandardError $workerStderr `
        -NoNewWindow `
        -PassThru
    Start-Sleep -Milliseconds 1500
    if ($workerProcess.HasExited) {
        throw "The short-SLA Worker failed to start."
    }

    $testId = [Guid]::NewGuid().ToString("N")
    $testLabel = "e2e-keep-$($testId.Substring(0, 12))"
    $label = Invoke-AccountChatwootJson `
        -Method Post `
        -Path "/api/v1/accounts/$accountId/labels" `
        -Step "Create temporary label" `
        -Body @{
            title = $testLabel
            description = "Temporary human handoff E2E label"
            color = "#64748B"
            show_on_sidebar = $false
        }
    if ($null -ne $label.id) {
        $testLabelId = [int]$label.id
    }
    elseif ($null -ne $label.payload -and $null -ne $label.payload.id) {
        $testLabelId = [int]$label.payload.id
    }

    $widgetConfig = Invoke-WidgetChatwootJson `
        -Path "/api/v1/widget/config" `
        -Headers @{ Accept = "application/json" } `
        -Step "Create temporary Widget contact" `
        -Body @{ website_token = $websiteToken }
    $contactId = [int]$widgetConfig.contact.id
    $widgetAuthToken = [string]$widgetConfig.website_channel_config.auth_token
    if ($contactId -le 0 -or [string]::IsNullOrWhiteSpace($widgetAuthToken)) {
        throw "Chatwoot did not create the temporary Widget contact."
    }
    $widgetHeaders = @{ Accept = "application/json"; "X-Auth-Token" = $widgetAuthToken }

    $conversation = Invoke-WidgetChatwootJson `
        -Path "/api/v1/widget/conversations" `
        -Headers $widgetHeaders `
        -Step "Create handoff conversation" `
        -Body @{
            website_token = $websiteToken
            contact = @{ name = "Human Handoff E2E" }
            message = @{
                content = $handoffQuestion
                echo_id = "handoff-$testId"
                referer_url = "http://localhost/human-handoff-e2e"
                timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            }
        }
    $conversationId = [int]$conversation.id
    if ($conversationId -le 0) {
        throw "Chatwoot did not create the handoff conversation."
    }

    Wait-ForLabel -ConversationId $conversationId -Label "ai_handoff"
    $labels = @(Get-ConversationLabels -ConversationId $conversationId)
    $preservedLabels = @($labels + $testLabel | Sort-Object -Unique)
    Invoke-AccountChatwootJson `
        -Method Post `
        -Path "/api/v1/accounts/$accountId/conversations/$conversationId/labels" `
        -Step "Attach preserved test label" `
        -Body @{ labels = $preservedLabels } | Out-Null

    $cancelledFaqId = Send-WidgetMessage -Content $faqQuestion -EchoId "cancel-$testId"
    Invoke-AccountChatwootJson `
        -Method Post `
        -Path "/api/v1/accounts/$accountId/conversations/$conversationId/messages" `
        -Step "Send timely human reply" `
        -Body @{
            content = $humanReply
            message_type = "outgoing"
            private = $false
            content_type = "text"
        } | Out-Null
    Assert-NoSourceMessage `
        -ConversationId $conversationId `
        -SourceId "ai-cs:${cancelledFaqId}:human-fallback" `
        -WaitSeconds 4

    $fallbackFaqId = Send-WidgetMessage -Content $faqQuestion -EchoId "fallback-$testId"
    $fallback = Wait-ForSourceMessage `
        -ConversationId $conversationId `
        -SourceId "ai-cs:${fallbackFaqId}:human-fallback" `
        -TimeoutSeconds $MessageTimeoutSeconds
    if ([string]$fallback.content -notmatch [Regex]::Escape($temporaryAiText)) {
        throw "Normal FAQ timeout did not produce a temporary AI answer."
    }

    $highRiskId = Send-WidgetMessage -Content $highRiskQuestion -EchoId "risk-$testId"
    $highRiskFallback = Wait-ForSourceMessage `
        -ConversationId $conversationId `
        -SourceId "ai-cs:${highRiskId}:human-fallback" `
        -TimeoutSeconds $MessageTimeoutSeconds
    if ([string]$highRiskFallback.content -notmatch [Regex]::Escape($humanServiceText)) {
        throw "High-risk timeout did not produce a human wait notice."
    }
    if ([string]$highRiskFallback.content -match $businessConclusionPattern) {
        throw "High-risk timeout returned an unsafe business conclusion."
    }

    $oldSourceIds = @(
        "ai-cs:${cancelledFaqId}:human-fallback",
        "ai-cs:${fallbackFaqId}:human-fallback",
        "ai-cs:${highRiskId}:human-fallback"
    )
    $oldSourceCounts = @{}
    foreach ($sourceId in $oldSourceIds) {
        $oldSourceCounts[$sourceId] = @(
            Get-ConversationMessages -ConversationId $conversationId | Where-Object {
                [string]$_.source_id -eq $sourceId
            }
        ).Count
    }

    $resume = Wait-ForAiResume -ConversationId $conversationId
    if ([string]$resume.status -ne "resumed") {
        throw "Operations API did not resume AI ownership."
    }
    $labelsAfterResume = @(Get-ConversationLabels -ConversationId $conversationId)
    if ($labelsAfterResume -contains "ai_handoff") {
        throw "Resume left the AI handoff label attached."
    }
    if ($labelsAfterResume -notcontains $testLabel) {
        throw "Resume removed an unrelated conversation label."
    }

    $newFaqId = Send-WidgetMessage -Content $faqQuestion -EchoId "resume-$testId"
    $standardReply = Wait-ForSourceMessage `
        -ConversationId $conversationId `
        -SourceId "ai-cs:${newFaqId}:reply" `
        -TimeoutSeconds $MessageTimeoutSeconds
    if ([string]$standardReply.content -match [Regex]::Escape($temporaryAiText)) {
        throw "The first post-resume FAQ was still treated as a temporary fallback."
    }

    Start-Sleep -Seconds 4
    $messagesAfterResume = @(Get-ConversationMessages -ConversationId $conversationId)
    foreach ($sourceId in $oldSourceIds) {
        $actualCount = @($messagesAfterResume | Where-Object {
            [string]$_.source_id -eq $sourceId
        }).Count
        if ($actualCount -ne [int]$oldSourceCounts[$sourceId]) {
            throw "A pre-resume customer message received a late AI answer."
        }
    }

    Write-Output (
        "PASS: Timely human reply cancellation, safe SLA fallback, " +
        "AI resume, label preservation, and old-message cutoff are verified."
    )
}
finally {
    if ($conversationId -gt 0) {
        try {
            Invoke-AccountChatwootJson `
                -Method Delete `
                -Path "/api/v1/accounts/$accountId/conversations/$conversationId" `
                -Step "Delete temporary conversation" | Out-Null
        }
        catch {}
    }
    if ($contactId -gt 0) {
        try {
            Invoke-AccountChatwootJson `
                -Method Delete `
                -Path "/api/v1/accounts/$accountId/contacts/$contactId" `
                -Step "Delete temporary contact" | Out-Null
        }
        catch {}
    }
    if ($testLabelId -gt 0) {
        try {
            Invoke-AccountChatwootJson `
                -Method Delete `
                -Path "/api/v1/accounts/$accountId/labels/$testLabelId" `
                -Step "Delete temporary label" | Out-Null
        }
        catch {}
    }

    Stop-ShortWorker -Process $workerProcess
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $savedEnvironment[$name],
            [EnvironmentVariableTarget]::Process
        )
    }
    foreach ($path in @($workerStdout, $workerStderr)) {
        if (Test-Path -LiteralPath $path) {
            try { Remove-Item -LiteralPath $path -Force } catch {}
        }
    }

    if ($composeWorkerWasRunning) {
        Push-Location $envDirectory
        try {
            $previousErrorAction = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                & docker compose up -d --no-deps worker *> $null
                $restartExitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previousErrorAction
            }
            if ($restartExitCode -ne 0) {
                Write-Warning "The regular Compose Worker must be restarted manually."
            }
        }
        finally {
            Pop-Location
        }
    }

    $websiteToken = $null
    $apiToken = $null
    $operationsKey = $null
    $widgetAuthToken = $null
    $widgetHeaders = $null
    $accountHeaders = $null
    $operationsHeaders = $null
    $configuration.Clear()
}
