param(
    [string]$BaseUrl = "http://127.0.0.1:5001",
    [string]$ApiKey = $env:OPERATIONS_API_KEY
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "OPERATIONS_API_KEY or -ApiKey is required for the protected conversion API."
}
$headers = @{ Authorization = "Bearer $ApiKey" }

try {
    $docsRequest = @{
        Uri = "$BaseUrl/docs"
        Headers = $headers
        UseBasicParsing = $true
        TimeoutSec = 10
    }
    $docsResponse = Invoke-WebRequest @docsRequest
} catch {
    throw "Docling API is not reachable at $BaseUrl. $($_.Exception.Message)"
}

if ($docsResponse.StatusCode -ne 200) {
    throw "Docling API docs returned HTTP $($docsResponse.StatusCode), expected 200."
}

$heading = -join @(0x552E, 0x540E, 0x653F, 0x7B56 | ForEach-Object { [char]$_ })
$bodyText = -join @(
    0x666E, 0x901A, 0x5546, 0x54C1, 0x7B7E, 0x6536, 0x540E,
    0x4E03, 0x5929, 0x5185, 0x652F, 0x6301, 0x65E0, 0x7406,
    0x7531, 0x9000, 0x8D27, 0x3002 | ForEach-Object { [char]$_ }
)
$sampleHtml = "<!doctype html><html><body><h1>$heading</h1><p>$bodyText</p></body></html>"

$payload = @{
    file_sources = @(
        @{
            base64_string = [Convert]::ToBase64String(
                [Text.Encoding]::UTF8.GetBytes($sampleHtml)
            )
            filename = "smoke-test.html"
        }
    )
    options = @{
        from_formats = @("html")
        to_formats = @("md")
        do_ocr = $false
    }
} | ConvertTo-Json -Depth 8

$conversionRequest = @{
    Uri = "$BaseUrl/v1/convert/source"
    Method = "Post"
    Headers = $headers
    ContentType = "application/json; charset=utf-8"
    Body = [Text.Encoding]::UTF8.GetBytes($payload)
    TimeoutSec = 120
}
$conversionResponse = Invoke-WebRequest @conversionRequest -UseBasicParsing
$resultJson = [Text.Encoding]::UTF8.GetString(
    $conversionResponse.RawContentStream.ToArray()
)
$result = $resultJson | ConvertFrom-Json

if ($result.status -notin @("success", "partial_success")) {
    throw "Docling conversion failed with status '$($result.status)'."
}

$markdown = $result.document.md_content
if (-not $markdown -or $markdown -notmatch [Regex]::Escape($heading)) {
    throw "Docling conversion response did not preserve the expected Unicode heading."
}
if ($markdown -notmatch [Regex]::Escape($bodyText)) {
    throw "Docling conversion response did not preserve the expected Unicode body."
}

Write-Output "Docling smoke test passed."
Write-Output "Status: $($result.status)"
Write-Output "Markdown length: $($markdown.Length)"
