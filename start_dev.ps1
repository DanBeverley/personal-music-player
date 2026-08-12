Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Starting EBB Development Environment" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

function Set-EnvFromDotEnv {
  param(
    [string]$Path,
    [switch]$OverrideExisting
  )

  if (-not (Test-Path $Path)) {
    return
  }

  Get-Content $Path | ForEach-Object {
    $line = $_.Trim()
    if ([string]::IsNullOrWhiteSpace($line)) {
      return
    }
    if ($line.StartsWith("#")) {
      return
    }

    $splitIndex = $line.IndexOf("=")
    if ($splitIndex -lt 1) {
      return
    }

    $key = $line.Substring(0, $splitIndex).Trim()
    $value = $line.Substring($splitIndex + 1).Trim()
    if ([string]::IsNullOrWhiteSpace($key)) {
      return
    }

    if (
      ($value.StartsWith('"') -and $value.EndsWith('"')) -or
      ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
      $value = $value.Substring(1, $value.Length - 2)
    }

    $existing = [Environment]::GetEnvironmentVariable($key, "Process")
    if ($OverrideExisting -or [string]::IsNullOrWhiteSpace($existing)) {
      [Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
  }
}

function Stop-RepoPythonProcesses {
  param(
    [string]$RepoRoot
  )

  $escapedRoot = [Regex]::Escape($RepoRoot)
  $processes = Get-CimInstance Win32_Process | Where-Object {
    $commandLine = $_.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
      return $false
    }

    return (
      $commandLine -match $escapedRoot -and (
        $commandLine -match "worker_main\.py" -or
        $commandLine -match "recommendation_worker\.py" -or
        $commandLine -match "uvicorn\s+api_app:app" -or
        $commandLine -match "uvicorn\s+server:app"
      )
    )
  }

  foreach ($process in $processes) {
    try {
      Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
      Write-Host ("Stopped stale backend process PID " + $process.ProcessId) -ForegroundColor DarkGray
    } catch {
      Write-Host ("Could not stop PID " + $process.ProcessId + ": " + $_.Exception.Message) -ForegroundColor DarkYellow
    }
  }
}

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-EnvFromDotEnv (Join-Path $RepoRoot ".env") -OverrideExisting
Stop-RepoPythonProcesses $RepoRoot

$ProxyPort = 8000
$AndroidEmulatorProxyUrl = "http://10.0.2.2:$ProxyPort"
$ClientProxyUrl = if ($env:AURALIS_PROXY_URL) {
  $env:AURALIS_PROXY_URL
} elseif ($env:AURALIS_DEVICE_PROXY_URL) {
  $env:AURALIS_DEVICE_PROXY_URL
} else {
  $AndroidEmulatorProxyUrl
}
$OllamaBaseUrl = if ($env:OLLAMA_BASE_URL) { $env:OLLAMA_BASE_URL } else { "http://127.0.0.1:11434/api" }
$OllamaModel = if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { "minimax-m2.7:cloud" }
$OllamaEmbedModel = if ($env:OLLAMA_EMBED_MODEL) { $env:OLLAMA_EMBED_MODEL } else { $OllamaModel }
$AssistantEmbedBackend = if ($env:ASSISTANT_EMBED_BACKEND) { $env:ASSISTANT_EMBED_BACKEND } else { "local" }
$AssistantVectorBackend = if ($env:ASSISTANT_VECTOR_BACKEND) { $env:ASSISTANT_VECTOR_BACKEND } else { "sqlite" }
$UseLangGraphAssistant = if ($env:USE_LANGGRAPH_ASSISTANT) { $env:USE_LANGGRAPH_ASSISTANT } else { "1" }
$RecommendationSyncDsn = if ($env:RECOMMENDATION_SYNC_DATABASE_DSN) { $env:RECOMMENDATION_SYNC_DATABASE_DSN } elseif ($env:DATABASE_URL) { $env:DATABASE_URL } else { "" }
$RecommendationEnableScheduler = if ($env:RECOMMENDATION_ENABLE_SCHEDULER) { $env:RECOMMENDATION_ENABLE_SCHEDULER } else { "1" }
$RecommendationSyncInterval = if ($env:RECOMMENDATION_SYNC_INTERVAL_SECONDS) { $env:RECOMMENDATION_SYNC_INTERVAL_SECONDS } else { "300" }
$RecommendationTrainInterval = if ($env:RECOMMENDATION_TRAIN_INTERVAL_SECONDS) { $env:RECOMMENDATION_TRAIN_INTERVAL_SECONDS } else { "900" }
$RecommendationEvalInterval = if ($env:RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS) { $env:RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS } else { "900" }
$RecommendationSyncFailureRetry = if ($env:RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS) { $env:RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS } else { "300" }
$RecommendationEvalWindowHours = if ($env:RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS) { $env:RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS } else { "168" }
$RecommendationMinImpressions = if ($env:RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS) { $env:RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS } else { "120" }
$RecommendationMinMargin = if ($env:RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN) { $env:RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN } else { "0.03" }
$RecommendationPromoteWinner = if ($env:RECOMMENDATION_PROMOTE_WINNER) { $env:RECOMMENDATION_PROMOTE_WINNER } else { "1" }
$RecommendationAttributionWindow = if ($env:RECOMMENDATION_ATTRIBUTION_WINDOW_SECONDS) { $env:RECOMMENDATION_ATTRIBUTION_WINDOW_SECONDS } else { "172800" }
$RecommendationModelExportDir = if ($env:RECOMMENDATION_MODEL_EXPORT_DIR) { $env:RECOMMENDATION_MODEL_EXPORT_DIR } else { "" }
$AuralisRuntimeDir = if ($env:AURALIS_RUNTIME_DIR) { $env:AURALIS_RUNTIME_DIR } else { Join-Path $RepoRoot "runtime" }
$AuralisDisableTimeouts = if ($env:AURALIS_DISABLE_TIMEOUTS) { $env:AURALIS_DISABLE_TIMEOUTS } else { "0" }
$RecommendationSyncStatus = if ([string]::IsNullOrWhiteSpace($RecommendationSyncDsn)) { "disabled" } else { "configured" }
$RecommendationSyncMode = if ($RecommendationSyncDsn -match "@db\..*\.supabase\.co(?::5432)?/") { "direct" } elseif ($RecommendationSyncDsn -match "pooler|supavisor") { "pooler" } else { "custom" }
$StartRecommendationWorker = $RecommendationEnableScheduler -notin @("0", "false", "False", "no", "No")
$RecommendationExternalWorker = if ($StartRecommendationWorker) { "1" } else { "0" }
$ProxyRecommendationScheduler = if ($StartRecommendationWorker) { "0" } else { $RecommendationEnableScheduler }
New-Item -ItemType Directory -Force -Path $AuralisRuntimeDir | Out-Null

if ($RecommendationSyncMode -eq "direct") {
  Write-Host "WARNING: RECOMMENDATION_SYNC_DATABASE_DSN appears to use the direct Supabase host. On IPv4-only networks this may fail; prefer the Supabase session pooler DSN." -ForegroundColor DarkYellow
}

if ($StartRecommendationWorker) {
  Write-Host "[1/3] Booting Recommendation Worker..." -ForegroundColor Yellow
  Start-Process powershell -ArgumentList "-NoProfile", "-NoExit", "-Command", "`$env:AURALIS_RUNTIME_DIR='$AuralisRuntimeDir'; `$env:OLLAMA_BASE_URL='$OllamaBaseUrl'; `$env:OLLAMA_MODEL='$OllamaModel'; `$env:OLLAMA_EMBED_MODEL='$OllamaEmbedModel'; `$env:ASSISTANT_EMBED_BACKEND='$AssistantEmbedBackend'; `$env:ASSISTANT_VECTOR_BACKEND='$AssistantVectorBackend'; `$env:USE_LANGGRAPH_ASSISTANT='$UseLangGraphAssistant'; `$env:RECOMMENDATION_SYNC_DATABASE_DSN='$RecommendationSyncDsn'; `$env:RECOMMENDATION_ENABLE_SCHEDULER='1'; `$env:RECOMMENDATION_EXTERNAL_WORKER='1'; `$env:RECOMMENDATION_SYNC_INTERVAL_SECONDS='$RecommendationSyncInterval'; `$env:RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS='$RecommendationSyncFailureRetry'; `$env:RECOMMENDATION_TRAIN_INTERVAL_SECONDS='$RecommendationTrainInterval'; `$env:RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS='$RecommendationEvalInterval'; `$env:RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS='$RecommendationEvalWindowHours'; `$env:RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS='$RecommendationMinImpressions'; `$env:RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN='$RecommendationMinMargin'; `$env:RECOMMENDATION_PROMOTE_WINNER='$RecommendationPromoteWinner'; `$env:RECOMMENDATION_ATTRIBUTION_WINDOW_SECONDS='$RecommendationAttributionWindow'; `$env:RECOMMENDATION_MODEL_EXPORT_DIR='$RecommendationModelExportDir'; `$env:AURALIS_DISABLE_TIMEOUTS='$AuralisDisableTimeouts'; cd ingestion\python_proxy; Write-Host 'EBB Recommendation Worker' -ForegroundColor Cyan; Write-Host ('RECOMMENDATION SYNC DSN ' + '$RecommendationSyncStatus' + ' (' + '$RecommendationSyncMode' + ')') -ForegroundColor DarkGray; Write-Host ('TIMEOUTS DISABLED ' + `$env:AURALIS_DISABLE_TIMEOUTS) -ForegroundColor DarkGray; Write-Host ('MAINTENANCE sync=' + `$env:RECOMMENDATION_SYNC_INTERVAL_SECONDS + 's retry=' + `$env:RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS + 's train=' + `$env:RECOMMENDATION_TRAIN_INTERVAL_SECONDS + 's eval=' + `$env:RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS + 's') -ForegroundColor DarkGray; Write-Host ('PROMOTION impressions=' + `$env:RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS + ' margin=' + `$env:RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN + ' enabled=' + `$env:RECOMMENDATION_PROMOTE_WINNER) -ForegroundColor DarkGray; python worker_main.py"
  Write-Host "[2/3] Booting Python Proxy Server on Port $ProxyPort..." -ForegroundColor Yellow
} else {
  Write-Host "[1/2] Booting Python Proxy Server on Port $ProxyPort..." -ForegroundColor Yellow
}

Start-Process powershell -ArgumentList "-NoProfile", "-NoExit", "-Command", "`$env:AURALIS_RUNTIME_DIR='$AuralisRuntimeDir'; `$env:OLLAMA_BASE_URL='$OllamaBaseUrl'; `$env:OLLAMA_MODEL='$OllamaModel'; `$env:OLLAMA_EMBED_MODEL='$OllamaEmbedModel'; `$env:ASSISTANT_EMBED_BACKEND='$AssistantEmbedBackend'; `$env:ASSISTANT_VECTOR_BACKEND='$AssistantVectorBackend'; `$env:USE_LANGGRAPH_ASSISTANT='$UseLangGraphAssistant'; `$env:RECOMMENDATION_SYNC_DATABASE_DSN='$RecommendationSyncDsn'; `$env:RECOMMENDATION_ENABLE_SCHEDULER='$ProxyRecommendationScheduler'; `$env:RECOMMENDATION_EXTERNAL_WORKER='$RecommendationExternalWorker'; `$env:RECOMMENDATION_SYNC_INTERVAL_SECONDS='$RecommendationSyncInterval'; `$env:RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS='$RecommendationSyncFailureRetry'; `$env:RECOMMENDATION_TRAIN_INTERVAL_SECONDS='$RecommendationTrainInterval'; `$env:RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS='$RecommendationEvalInterval'; `$env:RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS='$RecommendationEvalWindowHours'; `$env:RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS='$RecommendationMinImpressions'; `$env:RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN='$RecommendationMinMargin'; `$env:RECOMMENDATION_PROMOTE_WINNER='$RecommendationPromoteWinner'; `$env:RECOMMENDATION_ATTRIBUTION_WINDOW_SECONDS='$RecommendationAttributionWindow'; `$env:RECOMMENDATION_MODEL_EXPORT_DIR='$RecommendationModelExportDir'; `$env:AURALIS_DISABLE_TIMEOUTS='$AuralisDisableTimeouts'; cd ingestion\python_proxy; Write-Host 'EBB Python Engine' -ForegroundColor Cyan; Write-Host ('OLLAMA ' + `$env:OLLAMA_MODEL + ' @ ' + `$env:OLLAMA_BASE_URL) -ForegroundColor DarkGray; Write-Host ('EMBED MODEL ' + `$env:OLLAMA_EMBED_MODEL) -ForegroundColor DarkGray; Write-Host ('EMBED BACKEND ' + `$env:ASSISTANT_EMBED_BACKEND) -ForegroundColor DarkGray; Write-Host ('VECTOR BACKEND ' + `$env:ASSISTANT_VECTOR_BACKEND) -ForegroundColor DarkGray; Write-Host ('LANGGRAPH ' + `$env:USE_LANGGRAPH_ASSISTANT) -ForegroundColor DarkGray; Write-Host ('RECOMMENDATION SYNC DSN ' + '$RecommendationSyncStatus' + ' (' + '$RecommendationSyncMode' + ')') -ForegroundColor DarkGray; Write-Host ('RECOMMENDATION WORKER external=' + `$env:RECOMMENDATION_EXTERNAL_WORKER + ' scheduler=' + `$env:RECOMMENDATION_ENABLE_SCHEDULER) -ForegroundColor DarkGray; Write-Host ('TIMEOUTS DISABLED ' + `$env:AURALIS_DISABLE_TIMEOUTS) -ForegroundColor DarkGray; python -m uvicorn api_app:app --host 0.0.0.0 --port $ProxyPort --no-access-log"

# Start Flutter App in the current window
if ($StartRecommendationWorker) {
  Write-Host "[3/3] Booting Flutter Client with proxy $ClientProxyUrl..." -ForegroundColor Yellow
} else {
  Write-Host "[2/2] Booting Flutter Client with proxy $ClientProxyUrl..." -ForegroundColor Yellow
}
cd app
$dartDefines = @("--dart-define=AURALIS_PROXY_URL=$ClientProxyUrl")
$dartDefines += "--dart-define=AURALIS_DISABLE_TIMEOUTS=$AuralisDisableTimeouts"

if ($env:SUPABASE_URL) {
  $dartDefines += "--dart-define=SUPABASE_URL=$($env:SUPABASE_URL)"
}

if ($env:SUPABASE_ANON_KEY) {
  $dartDefines += "--dart-define=SUPABASE_ANON_KEY=$($env:SUPABASE_ANON_KEY)"
}

if ($env:SUPABASE_REDIRECT_SCHEME) {
  $dartDefines += "--dart-define=SUPABASE_REDIRECT_SCHEME=$($env:SUPABASE_REDIRECT_SCHEME)"
}

if ($env:SUPABASE_REDIRECT_HOST) {
  $dartDefines += "--dart-define=SUPABASE_REDIRECT_HOST=$($env:SUPABASE_REDIRECT_HOST)"
}

# Suppress the noisiest Android debug tags on the emulator itself. Do not set
# ANDROID_LOG_TAGS: recent Windows adb builds parse it as adb's own logging
# configuration and abort before Flutter can compile.
$AdbCommand = Get-Command adb -ErrorAction SilentlyContinue
if ($null -ne $AdbCommand) {
  & adb -s emulator-5554 shell setprop log.tag.EGL_emulation WARN 2>$null | Out-Null
  & adb -s emulator-5554 shell setprop log.tag.FlutterJNI WARN 2>$null | Out-Null
}

& flutter run -d emulator-5554 @dartDefines
