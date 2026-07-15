param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env")
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$dslTemplatePath = Join-Path $projectRoot "infra\dify\star-river-customer-service.yml"
$provisionerPath = Join-Path $PSScriptRoot "provision_dify_chatflow.py"
$containerName = "docker-api-1"
$operationId = [Guid]::NewGuid().ToString("N")
$containerDslPath = "/tmp/star-river-chatflow-$operationId.yml"
$containerProvisionerPath = "/tmp/provision-dify-chatflow-$operationId.py"
$containerSecretPath = "/tmp/dify-app-key-$operationId"
$renderedDslPath = Join-Path ([IO.Path]::GetTempPath()) "star-river-chatflow-$operationId.yml"
$localSecretPath = Join-Path ([IO.Path]::GetTempPath()) "dify-app-key-$operationId"

function Read-EnvValues {
    param([string]$Path)
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if ($line -match '^\s*([^#=][^=]*)=(.*)$') {
            $values[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    return $values
}

function Set-EnvValue {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [string]$Name,
        [string]$Value
    )
    $prefix = "$Name="
    for ($index = 0; $index -lt $Lines.Count; $index++) {
        if ($Lines[$index].StartsWith($prefix, [StringComparison]::Ordinal)) {
            $Lines[$index] = "$prefix$Value"
            return
        }
    }
    $Lines.Add("$prefix$Value")
}

if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw "Local .env file does not exist."
}
if (-not (Test-Path -LiteralPath $dslTemplatePath)) {
    throw "Dify Chatflow DSL does not exist."
}

$health = Invoke-WebRequest -Uri "http://127.0.0.1:8081/" -UseBasicParsing -TimeoutSec 15
if ($health.StatusCode -ne 200) {
    throw "Dify is not healthy."
}

$envValues = Read-EnvValues -Path $EnvPath
$datasetId = $envValues["DIFY_DATASET_ID"]
if ([string]::IsNullOrWhiteSpace($datasetId)) {
    throw "DIFY_DATASET_ID is not configured."
}

try {
    $dslTemplate = [IO.File]::ReadAllText($dslTemplatePath, [Text.Encoding]::UTF8)
    $renderedDsl = $dslTemplate.Replace("__DIFY_DATASET_ID__", $datasetId)
    [IO.File]::WriteAllText($renderedDslPath, $renderedDsl, [Text.UTF8Encoding]::new($false))

    docker cp $renderedDslPath "${containerName}:$containerDslPath" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to copy the Dify DSL." }
    docker cp $provisionerPath "${containerName}:$containerProvisionerPath" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to copy the Dify provisioner." }

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $provisionOutput = docker exec --workdir /app/api -e PYTHONPATH=/app/api $containerName python $containerProvisionerPath --dsl $containerDslPath --secret-output $containerSecretPath 2>&1
    $provisionExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    if ($provisionExitCode -ne 0) {
        $diagnostic = (($provisionOutput | Select-Object -Last 20) -join [Environment]::NewLine)
        $diagnostic = $diagnostic -replace 'app-[A-Za-z0-9_-]+', 'app-[REDACTED]'
        $diagnostic = $diagnostic -replace 'dataset-[A-Za-z0-9_-]+', 'dataset-[REDACTED]'
        $diagnostic = $diagnostic -replace 'sk-[A-Za-z0-9_-]+', 'sk-[REDACTED]'
        throw "Dify Chatflow provisioning failed. $diagnostic"
    }
    $metadataLine = $provisionOutput | Where-Object { $_ -match '^\{"app_id"' } | Select-Object -Last 1
    if (-not $metadataLine) {
        throw "Dify Chatflow provisioning returned no metadata."
    }
    $metadata = $metadataLine | ConvertFrom-Json

    docker cp "${containerName}:$containerSecretPath" $localSecretPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to transfer the Dify app key." }
    $appKey = [IO.File]::ReadAllText($localSecretPath, [Text.Encoding]::UTF8).Trim()
    if ([string]::IsNullOrWhiteSpace($appKey)) {
        throw "Dify generated an empty app key."
    }

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.AddRange([IO.File]::ReadAllLines($EnvPath, [Text.Encoding]::UTF8))
    Set-EnvValue -Lines $lines -Name "DIFY_APP_ID" -Value ([string]$metadata.app_id)
    Set-EnvValue -Lines $lines -Name "DIFY_APP_API_KEY" -Value $appKey
    [IO.File]::WriteAllLines($EnvPath, $lines, [Text.UTF8Encoding]::new($false))

    Write-Output "PASS: Dify Chatflow is published and its app key is stored locally."
}
finally {
    $appKey = $null
    $envValues.Clear()
    Remove-Item -LiteralPath $renderedDslPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $localSecretPath -Force -ErrorAction SilentlyContinue
    docker exec --user root $containerName sh -c "rm -f '$containerDslPath' '$containerProvisionerPath' '$containerSecretPath'" 2>$null | Out-Null
}
