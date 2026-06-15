param(
    [Parameter(Mandatory = $true)]
    [string]$Tag,

    [string]$Repository = "DanBeverley/personal-music-player",

    [string]$ReleaseNotes = "",

    [ValidateSet("none", "build", "patch", "minor", "major")]
    [string]$Bump = "none",

    [string]$AuralisProxyUrl = "http://34.172.70.149",

    [string]$SupabaseUrl = "https://ywfhlxfkfonxyrltgidv.supabase.co",

    [string]$SupabaseAnonKey = "sb_publishable_zyHAjfe-3NNaFqCI6LGd_Q_lw0Ke4zU",

    [string]$SupabaseRedirectScheme = "ebb",

    [string]$SupabaseRedirectHost = "login-callback",

    [string[]]$ExtraDartDefine = @(),

    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$appRoot = Join-Path $repoRoot "app"
$pubspecPath = Join-Path $appRoot "pubspec.yaml"
$outputDir = Join-Path $appRoot "build\app\outputs\flutter-apk"

if (-not (Test-Path $pubspecPath)) {
    throw "Could not find app pubspec.yaml at $pubspecPath"
}

$versionLine = Select-String -Path $pubspecPath -Pattern '^version:\s*(.+)$' | Select-Object -First 1
if (-not $versionLine) {
    throw "pubspec.yaml does not contain a version line."
}

$version = $versionLine.Matches[0].Groups[1].Value.Trim()
$versionParts = $version -split '\+'
$versionName = $versionParts[0]
$versionCode = 0
if ($versionParts.Count -gt 1) {
    $versionCode = [int]$versionParts[1]
}
if ($versionCode -le 0) {
    throw "Version code must be greater than 0. Current version: $version"
}

if ($Bump -ne "none") {
    $nameParts = $versionName -split '\.'
    if ($nameParts.Count -lt 3) {
        throw "Automatic bump expects semantic version format like 1.2.3. Current versionName: $versionName"
    }
    $major = [int]$nameParts[0]
    $minor = [int]$nameParts[1]
    $patch = [int]$nameParts[2]
    switch ($Bump) {
        "major" {
            $major += 1
            $minor = 0
            $patch = 0
        }
        "minor" {
            $minor += 1
            $patch = 0
        }
        "patch" {
            $patch += 1
        }
        "build" {}
    }
    $versionCode += 1
    if ($Bump -ne "build") {
        $versionName = "$major.$minor.$patch"
    }
    $version = "$versionName+$versionCode"
    $pubspecContent = Get-Content -Path $pubspecPath
    $pubspecContent = $pubspecContent | ForEach-Object {
        if ($_ -match '^version:\s*') {
            "version: $version"
        } else {
            $_
        }
    }
    Set-Content -Path $pubspecPath -Value $pubspecContent -Encoding UTF8
    Write-Host "Bumped app version to $version"
}

if (-not $SkipBuild) {
    Push-Location $appRoot
    try {
        $dartDefines = @(
            "AURALIS_PROXY_URL=$AuralisProxyUrl",
            "SUPABASE_URL=$SupabaseUrl",
            "SUPABASE_ANON_KEY=$SupabaseAnonKey",
            "SUPABASE_REDIRECT_SCHEME=$SupabaseRedirectScheme",
            "SUPABASE_REDIRECT_HOST=$SupabaseRedirectHost"
        ) + $ExtraDartDefine
        $buildArgs = @("build", "apk", "--release", "--split-per-abi")
        foreach ($define in $dartDefines) {
            $buildArgs += "--dart-define=$define"
        }
        Write-Host "Running: flutter $($buildArgs -join ' ')"
        & flutter @buildArgs
    } finally {
        Pop-Location
    }
}

$apkFiles = @(
    "app-arm64-v8a-release.apk",
    "app-armeabi-v7a-release.apk",
    "app-x86_64-release.apk"
)

$assets = @()
foreach ($apkName in $apkFiles) {
    $apkPath = Join-Path $outputDir $apkName
    if (-not (Test-Path $apkPath)) {
        Write-Warning "Skipping missing APK: $apkPath"
        continue
    }
    $abi = $apkName -replace '^app-', ''
    $abi = $abi -replace '-release\.apk$', ''
    $hash = (Get-FileHash -Algorithm SHA256 -Path $apkPath).Hash.ToLowerInvariant()
    $size = (Get-Item $apkPath).Length
    $assets += [ordered]@{
        abi = $abi
        name = $apkName
        url = "https://github.com/$Repository/releases/download/$Tag/$apkName"
        sha256 = $hash
        sizeBytes = $size
    }
}

if ($assets.Count -eq 0) {
    throw "No APK files were found in $outputDir"
}

$manifest = [ordered]@{
    versionName = $versionName
    versionCode = $versionCode
    releaseNotes = $ReleaseNotes
    mandatory = $false
    assets = $assets
}

$manifestPath = Join-Path $outputDir "update.json"
$manifest | ConvertTo-Json -Depth 8 | Set-Content -Path $manifestPath -Encoding UTF8

Write-Host "Built release manifest:"
Write-Host $manifestPath
Write-Host ""
Write-Host "Upload these assets to GitHub release ${Tag}:"
foreach ($asset in $assets) {
    Write-Host "- $($asset.name)"
}
Write-Host "- update.json"
