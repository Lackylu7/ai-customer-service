param(
    [string]$ChatwootUrl = "http://127.0.0.1:3000",
    [string]$DifyUrl = "http://127.0.0.1:8081",
    [string]$AiUrl = "http://127.0.0.1:5001/health",
    [string]$WidgetUrl = "http://127.0.0.1:8000"
)

$services = @(
    @{ Name = "Chatwoot"; Url = $ChatwootUrl },
    @{ Name = "Dify"; Url = $DifyUrl },
    @{ Name = "AI service"; Url = $AiUrl },
    @{ Name = "Widget"; Url = $WidgetUrl }
)
$allOnline = $true
foreach ($service in $services) {
    $online = $false
    try {
        $response = Invoke-WebRequest -Uri $service.Url -UseBasicParsing -TimeoutSec 5
        $online = $response.StatusCode -ge 200 -and $response.StatusCode -lt 400
    }
    catch {
        $online = $false
    }
    if ($online) {
        Write-Output ("{0,-12} ONLINE" -f $service.Name)
    }
    else {
        Write-Output ("{0,-12} OFFLINE" -f $service.Name)
        $allOnline = $false
    }
}
if (-not $allOnline) { exit 1 }
