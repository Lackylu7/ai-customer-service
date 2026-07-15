param(
    [string[]]$Mirrors = @(
        "docker.1ms.run",
        "docker.m.daocloud.io"
    )
)

$ErrorActionPreference = "Stop"

$images = @(
    @{ Official = "busybox:latest"; Remote = "library/busybox:latest" },
    @{ Official = "postgres:15-alpine"; Remote = "library/postgres:15-alpine" },
    @{ Official = "redis:6-alpine"; Remote = "library/redis:6-alpine" },
    @{ Official = "nginx:latest"; Remote = "library/nginx:latest" },
    @{ Official = "ubuntu/squid:latest"; Remote = "ubuntu/squid:latest" },
    @{ Official = "semitechnologies/weaviate:1.27.0"; Remote = "semitechnologies/weaviate:1.27.0" },
    @{ Official = "langgenius/dify-api:1.15.0"; Remote = "langgenius/dify-api:1.15.0" },
    @{ Official = "langgenius/dify-web:1.15.0"; Remote = "langgenius/dify-web:1.15.0" },
    @{ Official = "langgenius/dify-plugin-daemon:0.6.3-local"; Remote = "langgenius/dify-plugin-daemon:0.6.3-local" },
    @{ Official = "langgenius/dify-sandbox:0.2.15"; Remote = "langgenius/dify-sandbox:0.2.15" }
)

foreach ($image in $images) {
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    docker image inspect $image.Official *> $null
    $imageExists = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $previousErrorActionPreference

    if ($imageExists) {
        Write-Output "Image already exists: $($image.Official)"
        continue
    }

    $pulled = $false
    foreach ($mirror in $Mirrors) {
        $mirrorImage = "$mirror/$($image.Remote)"
        Write-Output "Pulling $($image.Official) through $mirror"
        docker pull $mirrorImage
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Mirror failed for $($image.Official): $mirror"
            continue
        }

        docker tag $mirrorImage $image.Official
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to tag Dify image: $($image.Official)"
        }
        $pulled = $true
        break
    }

    if (-not $pulled) {
        throw "Failed to pull Dify image: $($image.Official)"
    }
}

Write-Output "All Dify images are ready."
