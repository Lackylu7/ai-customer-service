param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [switch]$Rotate
)

$ErrorActionPreference = "Stop"
$key = $null
$existing = $null

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

try {
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.AddRange([IO.File]::ReadAllLines($EnvPath, [Text.Encoding]::UTF8))
    $existing = $lines |
        Where-Object { $_.StartsWith("OPERATIONS_API_KEY=", [StringComparison]::Ordinal) } |
        Select-Object -First 1
    $existingValue = if ([string]::IsNullOrWhiteSpace($existing)) {
        ""
    }
    else {
        $existing.Substring("OPERATIONS_API_KEY=".Length).Trim()
    }

    if ($Rotate.IsPresent -or [string]::IsNullOrWhiteSpace($existingValue)) {
        $bytes = [byte[]]::new(32)
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $random.GetBytes($bytes)
        }
        finally {
            $random.Dispose()
        }
        $key = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    }
    else {
        $key = $existingValue
    }

    Set-EnvValue -Lines $lines -Name "OPERATIONS_API_KEY" -Value $key
    [IO.File]::WriteAllLines(
        $EnvPath,
        $lines,
        [Text.UTF8Encoding]::new($false)
    )

    Write-Output "PASS: Operations API key is configured."
}
finally {
    $key = $null
    $existing = $null
    $existingValue = $null
    $bytes = $null
}
