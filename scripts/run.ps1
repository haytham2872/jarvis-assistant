# Start JARVIS -- Qdrant memory, backend, and frontend
param(
    [switch]$NoWake,       # keyboard input only (no microphone required)
    [switch]$BackendOnly,  # start Docker + backend only, skip frontend
    [switch]$FrontendOnly, # start frontend only, skip Docker + backend
    [switch]$Prod,         # load frontend from production build (run build.ps1 first)
    [switch]$NoUI,         # terminal REPL client (no Electron, no TTS)
    [switch]$Quick         # daily-use mode: backend/frontend run hidden, logs\*.log instead of two popup windows
)
$ErrorActionPreference = "Continue"

$root        = Split-Path $PSScriptRoot -Parent
$python      = Join-Path $root ".venv\Scripts\python.exe"
$internalDir = Join-Path $PSScriptRoot "_internal"

if (-not (Test-Path $python)) {
    Write-Host "ERROR: .venv not found. Run .\scripts\setup.ps1 first." -ForegroundColor Red
    exit 1
}

# --- Quick mode: hidden windows, output to log files instead of popups -------
$logDir = $null
if ($Quick) {
    $logDir = Join-Path $root "logs"
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
}

# --- Docker + Qdrant ---------------------------------------------------------
if (-not $FrontendOnly) {
    cmd /c "docker info >nul 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Docker Desktop is not running. Start Docker Desktop first." -ForegroundColor Red
        exit 1
    }

    try {
        & "$internalDir\docker.ps1" -Root $root -Mode run
    } catch {
        Write-Host "ERROR: Qdrant failed to start: $_" -ForegroundColor Red
        exit 1
    }
}

# --- Backend -----------------------------------------------------------------
$backendProc = $null
if (-not $FrontendOnly) {
    Write-Host "Starting JARVIS backend..." -ForegroundColor Cyan
    $backendCmd = "& '$internalDir\backend.ps1' -Root '$root'"
    # NoUI forces --no-wake (input comes from cli.py via WebSocket) and disables TTS
    $effectiveNoWake = $NoWake -or $NoUI
    if ($effectiveNoWake) { $backendCmd += " -NoWake" }
    if ($NoUI) { $env:NO_TTS = "1" }
    if ($Quick) {
        $backendProc = Start-Process powershell `
            -WindowStyle Hidden `
            -ArgumentList @("-NoProfile", "-Command", $backendCmd) `
            -RedirectStandardOutput (Join-Path $logDir "backend.log") `
            -RedirectStandardError  (Join-Path $logDir "backend.err.log") `
            -PassThru
        Write-Host "Backend started hidden (PID $($backendProc.Id)) -- logs\backend.log" -ForegroundColor Green
    } else {
        $backendProc = Start-Process powershell -ArgumentList @("-NoExit", "-Command", $backendCmd) -PassThru
        Write-Host "Backend started (PID $($backendProc.Id))." -ForegroundColor Green
    }
    Start-Sleep -Seconds 3
}

# --- NoUI: terminal REPL client (skip Electron, run cli.py in this window) ---
if ($NoUI) {
    Write-Host ""
    Write-Host "JARVIS CLI mode - no UI, no TTS. Ctrl+C to exit." -ForegroundColor Green
    $bPid = if ($null -ne $backendProc) { $backendProc.Id } else { 0 }
    try {
        & $python (Join-Path $root "backend\cli.py")
    } finally {
        try {
            & "$internalDir\teardown.ps1" -Root $root -BackendPid $bPid -FrontendPid 0
        } catch {
            Write-Host "Teardown error: $_" -ForegroundColor Yellow
        }
    }
    exit
}

# --- Frontend ----------------------------------------------------------------
$frontendProc = $null
if (-not $BackendOnly) {
    $modeLabel = if ($Prod) { "production build" } else { "dev mode" }
    Write-Host "Starting JARVIS frontend ($modeLabel)..." -ForegroundColor Cyan
    $frontendCmd = "& '$internalDir\frontend.ps1' -Root '$root'"
    if ($Prod) { $frontendCmd += " -Prod" }
    if ($Quick) {
        $frontendProc = Start-Process powershell `
            -WindowStyle Hidden `
            -ArgumentList @("-NoProfile", "-Command", $frontendCmd) `
            -RedirectStandardOutput (Join-Path $logDir "frontend.log") `
            -RedirectStandardError  (Join-Path $logDir "frontend.err.log") `
            -PassThru
        Write-Host "Frontend started hidden (PID $($frontendProc.Id)) -- logs\frontend.log" -ForegroundColor Green
    } else {
        $frontendProc = Start-Process powershell -ArgumentList @("-NoExit", "-Command", $frontendCmd) -PassThru
        Write-Host "Frontend started (PID $($frontendProc.Id))." -ForegroundColor Green
    }
}

Write-Host ""
if ($Quick) {
    Write-Host "JARVIS is running quietly -- Alt+J to open the window, logs\ for output." -ForegroundColor Green
    Write-Host "Press Ctrl+C in this terminal to stop everything." -ForegroundColor Green
} else {
    Write-Host "JARVIS is running. Press Ctrl+C to stop everything." -ForegroundColor Green
}

# --- Ctrl+C handler -- teardown on exit --------------------------------------
try {
    while ($true) {
        Start-Sleep -Seconds 10
        # Exit cleanly if the backend crashes so teardown still runs
        if ($null -ne $backendProc -and $backendProc.HasExited) {
            Write-Host "Backend process exited unexpectedly." -ForegroundColor Yellow
            break
        }
    }
} finally {
    $bPid = if ($null -ne $backendProc)  { $backendProc.Id }  else { 0 }
    $fPid = if ($null -ne $frontendProc) { $frontendProc.Id } else { 0 }
    try {
        & "$internalDir\teardown.ps1" -Root $root -BackendPid $bPid -FrontendPid $fPid
    } catch {
        Write-Host "Teardown error: $_" -ForegroundColor Yellow
    }
}
