$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot "scripts\configure-enterprise.ps1"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("enterprise-config-test-" + [Guid]::NewGuid().ToString("N"))
$configPath = Join-Path $temporaryRoot "enterprise.json"
$invalidConfigPath = Join-Path $temporaryRoot "invalid.json"
$envPath = Join-Path $temporaryRoot ".env"

try {
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot "config\enterprise.example.json") -Destination $configPath

    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -ConfigPath $configPath -EnvPath $envPath -ValidateOnly 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or $output -notmatch 'Enterprise configuration and local prerequisites are valid') {
        throw "Enterprise validation-only mode did not pass."
    }
    if (Test-Path -LiteralPath $envPath) {
        throw "Enterprise validation-only mode created an env file."
    }

    $invalid = Get-Content -Raw -Encoding UTF8 $configPath | ConvertFrom-Json
    $invalid.company.PSObject.Properties.Remove('name')
    [IO.File]::WriteAllText(
        $invalidConfigPath,
        ($invalid | ConvertTo-Json -Depth 8),
        [Text.UTF8Encoding]::new($false)
    )
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $invalidOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -ConfigPath $invalidConfigPath -EnvPath $envPath -ValidateOnly 2>&1 | Out-String
    $invalidExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    if ($invalidExitCode -eq 0 -or $invalidOutput -notmatch 'company.name') {
        throw "Enterprise validation did not report the invalid field path."
    }
    if (Test-Path -LiteralPath $envPath) {
        throw "Invalid enterprise validation created an env file."
    }

    Write-Output "PASS: Enterprise validation is strict and has no side effects."
}
finally {
    if ($temporaryRoot.StartsWith([IO.Path]::GetTempPath(), [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
