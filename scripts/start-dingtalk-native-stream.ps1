param(
    [Parameter(Mandatory = $true)]
    [string]$UnifiedAppId,

    [Parameter(Mandatory = $true)]
    [string]$AllowedUserId,

    [string]$HandlerUserId = "",

    [string]$OperatorUserId = "",

    [string]$DomainHandlerId = "",

    [string]$RobotCode = "",

    [switch]$EnableAutoGroup,

    [string]$DwsProfile = "",

    [string]$DwsExecutable = "dws",

    [string]$CollaborationGroupPrefix = "$([char]0x5185)$([char]0x90E8)$([char]0x670D)$([char]0x52A1)$([char]0x534F)$([char]0x4F5C)",

    [string]$StateDatabasePath = ".runtime\customer-service\dingtalk.db"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ($DwsExecutable -eq "dws") {
    $resolvedDws = Get-Command "dws.cmd" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $resolvedDws) {
        $resolvedDws = Get-Command "dws" -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if (-not $resolvedDws) {
        throw "Unable to resolve the DWS executable."
    }
    $DwsExecutable = $resolvedDws.Source
}
if ([System.IO.Path]::GetExtension($DwsExecutable) -in @(".cmd", ".bat")) {
    $vendorDws = Join-Path (Split-Path (Split-Path $DwsExecutable -Parent) -Parent) `
        "dingtalk-workspace-cli\vendor\dws.exe"
    if (Test-Path -LiteralPath $vendorDws -PathType Leaf) {
        $DwsExecutable = (Resolve-Path -LiteralPath $vendorDws).Path
    }
}

$credentialJson = & $DwsExecutable dev app credentials get `
    --unified-app-id $UnifiedAppId `
    --format json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to load DingTalk app credentials."
}

$credential = $credentialJson | ConvertFrom-Json
if (-not $credential.appKey -or -not $credential.appSecret) {
    throw "DingTalk app credentials are incomplete."
}

$env:DINGTALK_CLIENT_ID = [string]$credential.appKey
$env:DINGTALK_CLIENT_SECRET = [string]$credential.appSecret
$env:DINGTALK_ALLOWED_USERS = $AllowedUserId
$env:DINGTALK_HANDLER_USERS = $HandlerUserId
$env:DINGTALK_OPERATOR_USERS = $OperatorUserId
$env:DINGTALK_DOMAIN_HANDLERS = $DomainHandlerId
if (-not $RobotCode) {
    $robotJson = & $DwsExecutable dev app robot get `
        --unified-app-id $UnifiedAppId `
        --format json
    if ($LASTEXITCODE -eq 0) {
        $robot = $robotJson | ConvertFrom-Json
        $RobotCode = [string]$robot.robotCode
    }
}
if (-not $RobotCode) {
    throw "DingTalk robotCode is unavailable."
}
$env:DINGTALK_ROBOT_CODE = $RobotCode
$env:DINGTALK_STATE_DB_PATH = $StateDatabasePath
$env:DINGTALK_AUTO_GROUP_ENABLED = if ($EnableAutoGroup) { "true" } else { "false" }
$env:DINGTALK_DWS_PROFILE = $DwsProfile
$env:DINGTALK_DWS_EXECUTABLE = $DwsExecutable
$env:DINGTALK_COLLABORATION_GROUP_PREFIX = $CollaborationGroupPrefix

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = "python"
}

& $python -m app.dingtalk_stream_agent
exit $LASTEXITCODE
