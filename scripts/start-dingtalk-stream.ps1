param(
    [Parameter(Mandatory = $true)]
    [string]$UnifiedAppId,

    [Parameter(Mandatory = $true)]
    [string]$AllowedUserId
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

& dws dev connect `
    --unified-app-id $UnifiedAppId `
    --channel custom `
    --agent-cmd "python -X utf8 -m app.dingtalk_mvp_agent" `
    --agent-workdir $projectRoot `
    --allowed-users $AllowedUserId `
    --user-rate-limit 10 `
    --reply-card=false `
    --agent-memory=false `
    --agent-timeout 90 `
    --agent-permission-mode ask `
    --agent-approval-mode ask `
    --format json

exit $LASTEXITCODE
