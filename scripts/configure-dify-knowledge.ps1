[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Guid]$DatasetId,

    [Security.SecureString]$ApiKey,

    [string]$EnvPath
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvPath) {
    $EnvPath = Join-Path $projectRoot ".env"
}

$examplePath = Join-Path (Split-Path -Parent $EnvPath) ".env.example"
if (-not (Test-Path -LiteralPath $EnvPath)) {
    if (-not (Test-Path -LiteralPath $examplePath)) {
        throw "Cannot create .env because .env.example was not found."
    }
    Copy-Item -LiteralPath $examplePath -Destination $EnvPath
}

if (-not $ApiKey) {
    $ApiKey = Read-Host "Paste the Dify Knowledge API key" -AsSecureString
}
if ($ApiKey.Length -eq 0) {
    throw "The Dify Knowledge API key cannot be empty."
}

$bstr = [IntPtr]::Zero
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ApiKey)
    $plainApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

    $settings = [ordered]@{
        DIFY_BASE_URL = "http://127.0.0.1:8081"
        DIFY_DATASET_ID = $DatasetId.ToString()
        DIFY_DATASET_API_KEY = $plainApiKey
        DIFY_INDEXING_TECHNIQUE = "economy"
    }
    $seen = @{}
    $updatedLines = foreach ($line in [IO.File]::ReadAllLines($EnvPath, [Text.Encoding]::UTF8)) {
        $replacement = $null
        foreach ($name in $settings.Keys) {
            if ($line.StartsWith("$name=")) {
                $replacement = "$name=$($settings[$name])"
                $seen[$name] = $true
                break
            }
        }

        if ($null -ne $replacement) {
            $replacement
        }
        else {
            $line
        }
    }

    foreach ($name in $settings.Keys) {
        if (-not $seen.ContainsKey($name)) {
            $updatedLines += "$name=$($settings[$name])"
        }
    }

    [IO.File]::WriteAllLines($EnvPath, $updatedLines, [Text.UTF8Encoding]::new($false))
}
finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $plainApiKey = $null
}

Write-Host "Dify knowledge settings saved to $EnvPath. The API key was not printed."
