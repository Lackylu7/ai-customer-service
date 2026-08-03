param(
    [string]$OutputPath = "artifacts\production\ai-customer-service-images.tar",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$images = @(
    "ai-customer-service/docling-api:0.1.0",
    "caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
)

if (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $projectRoot $OutputPath
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $OutputPath
[IO.Directory]::CreateDirectory($outputDirectory) | Out-Null

if ((Test-Path -LiteralPath $OutputPath) -and -not $Force.IsPresent) {
    throw "Output already exists. Use -Force to replace it: $OutputPath"
}

foreach ($image in $images) {
    & docker image inspect $image *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Required production image is missing: $image"
    }
}

$temporaryPath = "$OutputPath.partial-$([Guid]::NewGuid().ToString('N'))"
try {
    & docker image save --output $temporaryPath @images
    if ($LASTEXITCODE -ne 0) {
        throw "Docker could not export the production images."
    }

    $minimumBytes = 100MB
    if ((Get-Item -LiteralPath $temporaryPath).Length -lt $minimumBytes) {
        throw "Exported production image archive is unexpectedly small."
    }

    Move-Item -LiteralPath $temporaryPath -Destination $OutputPath -Force:$Force.IsPresent
    $hash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumPath = "$OutputPath.sha256"
    [IO.File]::WriteAllText(
        $checksumPath,
        "$hash  $([IO.Path]::GetFileName($OutputPath))`n",
        [Text.Encoding]::ASCII
    )

    Write-Output "Production images exported: $OutputPath"
    Write-Output "SHA-256 checksum: $checksumPath"
}
finally {
    if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}
