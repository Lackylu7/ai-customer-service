param(
    [string]$Version = "1.15.0",
    [int]$HttpPort = 8081,
    [int]$HttpsPort = 8443,
    [int]$WorkerAmount = 1
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot ".runtime"
$difyRoot = Join-Path $runtimeRoot "dify-$Version"
$dockerRoot = Join-Path $difyRoot "docker"
$composePath = Join-Path $dockerRoot "docker-compose.yaml"
$overridePath = Join-Path $projectRoot "infra\dify\docker-compose.local.yaml"
$envExamplePath = Join-Path $dockerRoot ".env.example"
$envPath = Join-Path $dockerRoot ".env"

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

if (-not (Test-Path -LiteralPath $difyRoot)) {
    git clone --depth 1 --branch $Version https://github.com/langgenius/dify.git $difyRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to clone Dify $Version."
    }
}

if (-not (Test-Path -LiteralPath $envExamplePath)) {
    throw "Dify environment template was not found: $envExamplePath"
}

if (Test-Path -LiteralPath $envPath) {
    Write-Output "Dify .env already exists; keeping the existing configuration."
} else {
    Copy-Item -LiteralPath $envExamplePath -Destination $envPath
    $content = [IO.File]::ReadAllText($envPath, [Text.Encoding]::UTF8)

    function Set-DifyEnvironmentValue {
        param(
            [string]$Name,
            [string]$Value
        )

        $pattern = "(?m)^$([Regex]::Escape($Name))=.*$"
        if (-not [Regex]::IsMatch($script:content, $pattern)) {
            throw "Dify environment variable was not found: $Name"
        }
        $script:content = [Regex]::Replace(
            $script:content,
            $pattern,
            "$Name=$Value",
            1
        )
    }

    Set-DifyEnvironmentValue "EXPOSE_NGINX_PORT" $HttpPort
    Set-DifyEnvironmentValue "EXPOSE_NGINX_SSL_PORT" $HttpsPort
    Set-DifyEnvironmentValue "CELERY_WORKER_AMOUNT" $WorkerAmount
    Set-DifyEnvironmentValue "COMPOSE_PROFILES" '${VECTOR_STORE:-weaviate},${DB_TYPE:-postgresql}'

    [IO.File]::WriteAllText($envPath, $content, [Text.UTF8Encoding]::new($false))
}

docker compose --project-directory $dockerRoot --env-file $envPath `
    -f $composePath -f $overridePath config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Dify Docker Compose configuration is invalid."
}

Write-Output "Dify $Version is ready at: $difyRoot"
Write-Output "Configured local URL: http://127.0.0.1:$HttpPort"
