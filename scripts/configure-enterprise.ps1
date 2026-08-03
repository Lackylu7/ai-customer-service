param(
    [string]$ConfigPath = "config\enterprise.example.json",
    [string]$EnvPath = ".env",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configCliPath = Join-Path $projectRoot "app\enterprise_config.py"
$envExamplePath = Join-Path $projectRoot ".env.example"
$provisionDifyPath = Join-Path $PSScriptRoot "provision-dify-chatflow.ps1"
$configureChatwootPath = Join-Path $PSScriptRoot "configure-chatwoot.ps1"
$configureOperationsPath = Join-Path $PSScriptRoot "configure-operations.ps1"
$runWidgetPath = Join-Path $PSScriptRoot "run-widget-demo.ps1"

function Resolve-ProjectPath {
    param([string]$Path)
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $Path))
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
$requiredFiles = @(
    $pythonPath,
    $configCliPath,
    $envExamplePath,
    $provisionDifyPath,
    $configureChatwootPath,
    $configureOperationsPath,
    $runWidgetPath,
    $resolvedConfigPath
)
foreach ($path in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Enterprise setup prerequisite is missing."
    }
}
if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is required."
}

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$validationOutput = & $pythonPath $configCliPath --path $resolvedConfigPath 2>&1
$validationExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($validationExitCode -ne 0) {
    throw (($validationOutput | Select-Object -Last 1) -join "")
}
if ($ValidateOnly.IsPresent) {
    Write-Output "PASS: Enterprise configuration and local prerequisites are valid."
    exit 0
}

if (-not (Test-Path -LiteralPath $resolvedEnvPath)) {
    Copy-Item -LiteralPath $envExamplePath -Destination $resolvedEnvPath
}
$lines = [System.Collections.Generic.List[string]]::new()
$lines.AddRange([IO.File]::ReadAllLines($resolvedEnvPath, [Text.Encoding]::UTF8))
Set-EnvValue -Lines $lines -Name "ENTERPRISE_CONFIG_PATH" -Value $ConfigPath
[IO.File]::WriteAllLines($resolvedEnvPath, $lines, [Text.UTF8Encoding]::new($false))

& $provisionDifyPath -ConfigPath $resolvedConfigPath -EnvPath $resolvedEnvPath
& $configureChatwootPath -ConfigPath $resolvedConfigPath -EnvPath $resolvedEnvPath
& $configureOperationsPath -EnvPath $resolvedEnvPath
& $runWidgetPath -ConfigPath $resolvedConfigPath -EnvPath $resolvedEnvPath -RenderOnly

Write-Output "PASS: Enterprise AI customer service is configured locally."
