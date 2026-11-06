[CmdletBinding()]
param(
  [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
  throw "Environment file was not found: $EnvPath"
}

$allowedExact = @(
  "DATABASE_URL",
  "LASTFM_API_KEY",
  "YOUTUBE_DATA_API_KEY",
  "AUDIUS_APP_NAME",
  "USE_LANGGRAPH_ASSISTANT"
)
$allowedPrefixes = @(
  "AURALIS_",
  "ASSISTANT_",
  "OLLAMA_",
  "PREPARE_",
  "RECOMMENDATION_",
  "SEARCH_",
  "STREAM_",
  "UPSTREAM_"
)
$denied = @(
  "AURALIS_PROXY_URL",
  "AURALIS_DEVICE_PROXY_URL",
  "AURALIS_RUNTIME_DIR",
  "AURALIS_PROXY_RUNTIME_DIR",
  "AURALIS_ASSISTANT_MEMORY_DB_PATH",
  "AURALIS_RECOMMENDATION_STORE_DB_PATH",
  "AURALIS_STREAM_CACHE_TMP_DIR",
  "AURALIS_YTDLP_COOKIES_PATH",
  "PORT"
)
$required = @(
  "RECOMMENDATION_SYNC_DATABASE_DSN",
  "AURALIS_STREAM_CACHE_BACKEND",
  "AURALIS_STREAM_CACHE_BUCKET",
  "AURALIS_R2_ACCOUNT_ID",
  "AURALIS_R2_ACCESS_KEY_ID",
  "AURALIS_R2_SECRET_ACCESS_KEY",
  "AURALIS_R2_ENDPOINT_URL",
  "YOUTUBE_DATA_API_KEY",
  "LASTFM_API_KEY"
)

$selected = [ordered]@{}
foreach ($rawLine in Get-Content -LiteralPath $EnvPath) {
  $line = $rawLine.Trim()
  if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
    continue
  }
  $separator = $line.IndexOf("=")
  $name = $line.Substring(0, $separator).Trim()
  $rawValue = $line.Substring($separator + 1).Trim()
  if ($name -notmatch "^[A-Z][A-Z0-9_]*$") {
    continue
  }
  $prefixAllowed = $false
  foreach ($prefix in $allowedPrefixes) {
    if ($name.StartsWith($prefix, [StringComparison]::Ordinal)) {
      $prefixAllowed = $true
      break
    }
  }
  if (($name -notin $allowedExact -and -not $prefixAllowed) -or $name -in $denied) {
    continue
  }
  $selected[$name] = $rawValue
}

$missing = @()
foreach ($name in $required) {
  if (-not $selected.Contains($name)) {
    $missing += $name
    continue
  }
  $value = [string]$selected[$name]
  $unquoted = $value.Trim().Trim('"').Trim("'")
  if ([string]::IsNullOrWhiteSpace($unquoted)) {
    $missing += $name
  }
}
if ($missing.Count -gt 0) {
  throw "Missing required production values in .env: $($missing -join ', ')"
}

$streamBackend = ([string]$selected["AURALIS_STREAM_CACHE_BACKEND"]).Trim().Trim('"').Trim("'")
if ($streamBackend -ne "r2") {
  throw "AURALIS_STREAM_CACHE_BACKEND must be r2 for production deployment."
}

$syncDsn = ([string]$selected["RECOMMENDATION_SYNC_DATABASE_DSN"]).Trim().Trim('"').Trim("'")
if ($syncDsn -match "@db\.[^.]+\.supabase\.co(?::5432)?/") {
  throw "RECOMMENDATION_SYNC_DATABASE_DSN uses the direct Supabase host. Use the Session Pooler DSN for an IPv4 Oracle VM."
}

$payloadLines = foreach ($entry in $selected.GetEnumerator()) {
  "$($entry.Key)=$($entry.Value)"
}
$payload = ($payloadLines -join "`n") + "`n"
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
Set-Clipboard -Value $encoded

Write-Host "Copied BACKEND_ENV_B64 to the clipboard." -ForegroundColor Green
Write-Host "Included $($selected.Count) approved variables; values were not printed." -ForegroundColor DarkGray
$selected.Keys | Sort-Object | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
Write-Host "Clear the clipboard after saving the GitHub secret: Set-Clipboard -Value ''" -ForegroundColor Yellow
