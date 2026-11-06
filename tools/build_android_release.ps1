param(
    [Parameter(Mandatory = $true)]
    [string]$Tag,

    [string]$Repository = "DanBeverley/personal-music-player",

    [string]$ReleaseNotes = "",

    [ValidateSet("none", "build", "patch", "minor", "major")]
    [string]$Bump = "none",

    [string]$AuralisProxyUrl = $env:AURALIS_PROXY_URL,

    [string]$SupabaseUrl = $env:SUPABASE_URL,

    [string]$SupabaseAnonKey = $env:SUPABASE_ANON_KEY,

    [string]$SupabaseRedirectScheme = $(
        if ($env:SUPABASE_REDIRECT_SCHEME) { $env:SUPABASE_REDIRECT_SCHEME } else { "ebb" }
    ),

    [string]$SupabaseRedirectHost = $(
        if ($env:SUPABASE_REDIRECT_HOST) { $env:SUPABASE_REDIRECT_HOST } else { "login-callback" }
    ),

    [string]$ExpectedSigningSha256 = "4968ebd1503e3b25d49641827fa2d1c63861b17698cf7f83410dd574e678646b",

    [string[]]$ExtraDartDefine = @(),

    [switch]$Mandatory,

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

$requiredConfig = [ordered]@{
    AURALIS_PROXY_URL = $AuralisProxyUrl
    SUPABASE_URL = $SupabaseUrl
    SUPABASE_ANON_KEY = $SupabaseAnonKey
    SUPABASE_REDIRECT_SCHEME = $SupabaseRedirectScheme
    SUPABASE_REDIRECT_HOST = $SupabaseRedirectHost
}
foreach ($entry in $requiredConfig.GetEnumerator()) {
    if ([string]::IsNullOrWhiteSpace([string]$entry.Value)) {
        throw "Missing release configuration: $($entry.Key)"
    }
}

$requiredSigning = @(
    "ANDROID_KEYSTORE_PATH",
    "ANDROID_KEY_ALIAS",
    "ANDROID_STORE_PASSWORD",
    "ANDROID_KEY_PASSWORD"
)
foreach ($name in $requiredSigning) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Missing Android release signing environment variable: $name"
    }
}
if (-not (Test-Path -LiteralPath $env:ANDROID_KEYSTORE_PATH)) {
    throw "Android release keystore was not found at ANDROID_KEYSTORE_PATH."
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

$expectedTag = "v$versionName"
if ($Tag -ne $expectedTag) {
    throw "Release tag must match pubspec versionName. Expected '$expectedTag', got '$Tag'."
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
        Write-Host "Building signed split-per-ABI Android release."
        & flutter @buildArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Flutter Android release build failed with exit code $LASTEXITCODE."
        }
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
        throw "Expected split APK is missing: $apkPath"
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

$androidSdkRoot = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { $env:ANDROID_SDK_ROOT }
if ([string]::IsNullOrWhiteSpace($androidSdkRoot)) {
    throw "ANDROID_HOME or ANDROID_SDK_ROOT must be set to verify APK signing."
}
$buildToolsRoot = Join-Path $androidSdkRoot "build-tools"
$buildTools = Get-ChildItem -LiteralPath $buildToolsRoot -Directory |
    Sort-Object Name -Descending |
    Select-Object -First 1
if (-not $buildTools) {
    throw "No Android build-tools installation was found under $buildToolsRoot."
}
$apksignerName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "apksigner.bat" } else { "apksigner" }
$apksigner = Join-Path $buildTools.FullName $apksignerName
if (-not (Test-Path -LiteralPath $apksigner)) {
    throw "apksigner was not found at $apksigner."
}
$normalizedExpectedSigningSha256 = ($ExpectedSigningSha256 -replace ":", "").ToLowerInvariant()
foreach ($apkName in $apkFiles) {
    $apkPath = Join-Path $outputDir $apkName
    $verificationOutput = & $apksigner verify --print-certs $apkPath 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "APK signature verification failed for ${apkName}: $verificationOutput"
    }
    $digestLine = $verificationOutput | Where-Object {
        $_ -match "Signer #1 certificate SHA-256 digest:\s*([a-fA-F0-9]+)"
    } | Select-Object -First 1
    if (-not $digestLine) {
        throw "Could not read the APK signing certificate fingerprint for $apkName."
    }
    $actualSigningSha256 = ([regex]::Match(
        [string]$digestLine,
        "Signer #1 certificate SHA-256 digest:\s*([a-fA-F0-9]+)"
    ).Groups[1].Value).ToLowerInvariant()
    if ($actualSigningSha256 -ne $normalizedExpectedSigningSha256) {
        throw "APK signing fingerprint mismatch for ${apkName}. Expected $normalizedExpectedSigningSha256, got $actualSigningSha256."
    }
}
Write-Host "Verified Android signing certificate on all APKs: $normalizedExpectedSigningSha256"

$manifest = [ordered]@{
    schemaVersion = 1
    packageName = "com.danbeverley.ebb"
    versionName = $versionName
    versionCode = $versionCode
    releaseNotes = $ReleaseNotes
    mandatory = [bool]$Mandatory
    publishedAt = [DateTime]::UtcNow.ToString("o")
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
