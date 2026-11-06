param(
    [string]$FlutterRoot = "$env:USERPROFILE\flutter",
    [string]$Python = 'python',
    [int]$Port = 8081,
    [int]$BackendPort = 8000,
    [switch]$Build
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if ($Build) {
    $dartExecutable = Join-Path $FlutterRoot 'bin\cache\dart-sdk\bin\dart.exe'
    $flutterSnapshot = Join-Path $FlutterRoot 'bin\cache\flutter_tools.snapshot'
    if (!(Test-Path $dartExecutable) -or !(Test-Path $flutterSnapshot)) {
        throw 'Flutter cache is missing. Pass -FlutterRoot with your installed SDK directory.'
    }
    Push-Location (Join-Path $projectRoot 'app')
    try {
        # Use the installed SDK directly; avoid flutter.bat startup Git fetches.
        & $dartExecutable $flutterSnapshot build web --no-pub --no-web-resources-cdn
        if ($LASTEXITCODE -ne 0) { throw 'Web build failed.' }
    } finally {
        Pop-Location
    }
}
try {
    $null = Invoke-WebRequest "http://127.0.0.1:$BackendPort/" -UseBasicParsing -TimeoutSec 5
} catch {
    throw "Start the music API on loopback port $BackendPort before launching the preview."
}
# Public preview: use HTTPS when exposing this loopback gateway remotely.
& $Python (Join-Path $PSScriptRoot 'serve_web_preview.py') --public --port $Port --backend-port $BackendPort
if ($LASTEXITCODE -ne 0) { throw 'Preview gateway failed.' }
