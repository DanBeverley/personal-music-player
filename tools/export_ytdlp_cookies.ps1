[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$CookiesPath
)

$ErrorActionPreference = "Stop"
$resolved = (Resolve-Path -LiteralPath $CookiesPath).Path
if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
  throw "Cookies file was not found: $CookiesPath"
}
$bytes = [IO.File]::ReadAllBytes($resolved)
if ($bytes.Length -eq 0) {
  throw "Cookies file is empty."
}
Set-Clipboard -Value ([Convert]::ToBase64String($bytes))
Write-Host "Copied YTDLP_COOKIES_B64 to the clipboard; file contents were not printed." -ForegroundColor Green
Write-Host "Clear the clipboard after saving the GitHub secret: Set-Clipboard -Value ''" -ForegroundColor Yellow
