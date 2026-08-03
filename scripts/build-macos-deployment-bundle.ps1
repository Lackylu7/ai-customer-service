param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [Parameter(Mandatory = $true)]
    [string]$ShareholderKnowledgePath,

    [Parameter(Mandatory = $true)]
    [string]$StoreExpansionKnowledgePath,

    [Parameter(Mandatory = $true)]
    [string]$OpenClawPackagePath
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $resolvedOutput) {
    throw "Output already exists: $resolvedOutput"
}

$requiredInputs = @(
    $ShareholderKnowledgePath,
    $StoreExpansionKnowledgePath,
    $OpenClawPackagePath
)
foreach ($inputPath in $requiredInputs) {
    if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf)) {
        throw "Required deployment input is missing: $inputPath"
    }
}

$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("ai-customer-service-macos-" + [guid]::NewGuid().ToString("N"))
$resolvedStagingRoot = [System.IO.Path]::GetFullPath($stagingRoot)
$resolvedTempRoot = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::GetTempPath()
)
if (-not $resolvedStagingRoot.StartsWith(
    $resolvedTempRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Unsafe staging path: $resolvedStagingRoot"
}

$bundleRoot = Join-Path $resolvedStagingRoot "ai-customer-service-deploy"
try {
    New-Item -ItemType Directory -Path $bundleRoot | Out-Null

    foreach ($directory in @("app", "docs", "infra", "openclaw", "scripts", "tests")) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $directory) `
            -Destination (Join-Path $bundleRoot $directory) -Recurse
    }

    $configTarget = Join-Path $bundleRoot "config"
    New-Item -ItemType Directory -Path $configTarget | Out-Null
    foreach ($configName in @(
        "enterprise.example.json",
        "enterprise.v05-test.json",
        "store-directory.example.json"
    )) {
        Copy-Item -LiteralPath (Join-Path $projectRoot "config\$configName") `
            -Destination (Join-Path $configTarget $configName)
    }

    foreach ($fileName in @(
        ".env.example",
        "README.md",
        "requirements-dingtalk.txt",
        "upgrade-macos.command",
        "upgrade-and-enable-openclaw.command"
    )) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $fileName) `
            -Destination (Join-Path $bundleRoot $fileName)
    }

    $knowledgeTarget = Join-Path $bundleRoot "knowledge-approved-sources"
    New-Item -ItemType Directory -Path $knowledgeTarget | Out-Null
    Copy-Item -LiteralPath $ShareholderKnowledgePath `
        -Destination (Join-Path $knowledgeTarget "enterprise-knowledge-shareholder.md")
    Copy-Item -LiteralPath $StoreExpansionKnowledgePath `
        -Destination (Join-Path $knowledgeTarget "shareholder-store-expansion-guidance.md")

    $packagesTarget = Join-Path $bundleRoot "packages"
    New-Item -ItemType Directory -Path $packagesTarget | Out-Null
    Copy-Item -LiteralPath $OpenClawPackagePath `
        -Destination (Join-Path $packagesTarget "shareholder-3-0-openclaw-package-v0.4.zip")

    Get-ChildItem -LiteralPath $bundleRoot -Recurse -Directory -Force |
        Where-Object { $_.Name -eq "__pycache__" } |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $bundleRoot -Recurse -File -Force |
        Where-Object { $_.Extension -eq ".pyc" } |
        Remove-Item -Force

    $forbidden = Get-ChildItem -LiteralPath $bundleRoot -Recurse -File -Force |
        Where-Object {
            $_.Name -in @(".env", "enterprise.local.json", "store-directory.local.json") -or
            $_.FullName -match "[\\/]\.runtime[\\/]" -or
            $_.FullName -match "[\\/]\.venv[\\/]"
        }
    if ($forbidden) {
        throw "Forbidden local files entered the deployment bundle."
    }

    $manifest = Get-ChildItem -LiteralPath $bundleRoot -Recurse -File -Force |
        Sort-Object FullName |
        ForEach-Object {
            [pscustomobject]@{
                path = $_.FullName.Substring($bundleRoot.Length + 1).Replace("\", "/")
                size = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            }
        }
    $manifest | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath (Join-Path $bundleRoot "deployment-manifest.json") `
            -Encoding utf8

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::Open(
        $resolvedOutput,
        [System.IO.Compression.ZipArchiveMode]::Create
    )
    try {
        Get-ChildItem -LiteralPath $bundleRoot -Recurse -File -Force |
            ForEach-Object {
                $relative = $_.FullName.Substring($bundleRoot.Length + 1).Replace("\", "/")
                $entryName = "ai-customer-service-deploy/$relative"
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $archive,
                    $_.FullName,
                    $entryName,
                    [System.IO.Compression.CompressionLevel]::Optimal
                ) | Out-Null
            }
    }
    finally {
        $archive.Dispose()
    }

    $hash = (Get-FileHash -LiteralPath $resolvedOutput -Algorithm SHA256).Hash
    [pscustomobject]@{
        output = $resolvedOutput
        sha256 = $hash
        files = @($manifest).Count + 1
    } | ConvertTo-Json -Compress
}
finally {
    if (
        (Test-Path -LiteralPath $resolvedStagingRoot) -and
        $resolvedStagingRoot.StartsWith(
            $resolvedTempRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Remove-Item -LiteralPath $resolvedStagingRoot -Recurse -Force
    }
}
