param(
    [string]$ConfigPath = "config\enterprise.example.json",
    [string]$EnvPath = ".env",
    [string]$FixturePath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configCliPath = Join-Path $projectRoot "app\enterprise_config.py"
$dslTemplatePath = Join-Path $projectRoot "infra\dify\customer-service-template.yml"
$provisionerPath = Join-Path $PSScriptRoot "provision_dify_chatflow.py"
$containerName = "docker-api-1"
$operationId = [Guid]::NewGuid().ToString("N")
$containerDslPath = "/tmp/customer-service-$operationId.yml"
$containerConfigPath = "/tmp/enterprise-$operationId.json"
$containerProvisionerPath = "/tmp/provision-dify-$operationId.py"
$containerSecretPath = "/tmp/dify-resources-$operationId.json"
$localSecretPath = Join-Path ([IO.Path]::GetTempPath()) "dify-resources-$operationId.json"

function Resolve-ProjectPath {
    param([string]$Path)
    if ([IO.Path]::IsPathRooted($Path)) {
        return [IO.Path]::GetFullPath($Path)
    }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $Path))
}

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

$resolvedConfigPath = Resolve-ProjectPath -Path $ConfigPath
$resolvedEnvPath = Resolve-ProjectPath -Path $EnvPath
$resolvedFixturePath = if ($FixturePath) { Resolve-ProjectPath -Path $FixturePath } else { "" }

foreach ($requiredPath in @($pythonPath, $configCliPath, $dslTemplatePath, $provisionerPath, $resolvedConfigPath, $resolvedEnvPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Dify enterprise provisioning prerequisite is missing."
    }
}

$configOutput = & $pythonPath $configCliPath --path $resolvedConfigPath --print-json 2>&1
if ($LASTEXITCODE -ne 0) {
    throw (($configOutput | Select-Object -Last 1) -join "")
}
$enterprise = ($configOutput -join [Environment]::NewLine) | ConvertFrom-Json
$metadata = $null
$envValues = Read-EnvValues -Path $resolvedEnvPath

try {
    if ($resolvedFixturePath) {
        if (-not (Test-Path -LiteralPath $resolvedFixturePath -PathType Leaf)) {
            throw "Dify fixture file does not exist."
        }
        $metadata = [IO.File]::ReadAllText($resolvedFixturePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    }
    else {
        $difyBaseUrl = [string]$envValues["DIFY_BASE_URL"]
        if ([string]::IsNullOrWhiteSpace($difyBaseUrl)) {
            $difyBaseUrl = "http://127.0.0.1:8081"
        }
        $health = Invoke-WebRequest -Uri $difyBaseUrl.TrimEnd('/') -UseBasicParsing -TimeoutSec 15
        if ($health.StatusCode -ne 200) {
            throw "Dify is not healthy."
        }

        foreach ($copy in @(
            @($dslTemplatePath, $containerDslPath),
            @($resolvedConfigPath, $containerConfigPath),
            @($provisionerPath, $containerProvisionerPath)
        )) {
            & docker cp $copy[0] "${containerName}:$($copy[1])" 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Failed to copy a Dify provisioning file." }
        }

        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $provisionOutput = & docker exec --workdir /app/api -e PYTHONPATH=/app/api `
            $containerName python $containerProvisionerPath `
            --dsl $containerDslPath `
            --enterprise-config $containerConfigPath `
            --secret-output $containerSecretPath 2>&1
        $provisionExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        if ($provisionExitCode -ne 0) {
            throw "Dify enterprise provisioning failed."
        }

        & docker cp "${containerName}:$containerSecretPath" $localSecretPath 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to transfer Dify resource metadata." }
        $metadata = [IO.File]::ReadAllText($localSecretPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    }

    $values = @{
        DIFY_DATASET_ID = [string]$metadata.dataset_id
        DIFY_DATASET_API_KEY = [string]$metadata.dataset_api_key
        DIFY_INDEXING_TECHNIQUE = [string]$enterprise.dify.indexing_technique
        DIFY_APP_ID = [string]$metadata.app_id
        DIFY_APP_API_KEY = [string]$metadata.app_api_key
    }
    foreach ($name in $values.Keys) {
        if ([string]::IsNullOrWhiteSpace($values[$name])) {
            throw "Dify enterprise provisioning returned incomplete metadata."
        }
    }

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.AddRange([IO.File]::ReadAllLines($resolvedEnvPath, [Text.Encoding]::UTF8))
    foreach ($name in $values.Keys) {
        Set-EnvValue -Lines $lines -Name $name -Value $values[$name]
    }
    [IO.File]::WriteAllLines($resolvedEnvPath, $lines, [Text.UTF8Encoding]::new($false))
    Write-Output "PASS: Dify knowledge base and Chatflow are configured for the enterprise."
}
finally {
    $metadata = $null
    $envValues.Clear()
    Remove-Item -LiteralPath $localSecretPath -Force -ErrorAction SilentlyContinue
    if (-not $resolvedFixturePath) {
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & docker exec --user root $containerName rm -f `
            $containerDslPath $containerConfigPath $containerProvisionerPath $containerSecretPath `
            2>$null | Out-Null
        $ErrorActionPreference = $previousErrorAction
    }
}
