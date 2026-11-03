# Internal -- do not run directly. Called by run.ps1 on Ctrl+C to gracefully stop JARVIS.
param(
    [Parameter(Mandatory)][string]$Root,
    [int]$BackendPid  = 0,
    [int]$FrontendPid = 0
)
$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "Stopping JARVIS..." -ForegroundColor Cyan

if ($BackendPid -gt 0) {
    # Step 1: flush memory before killing.
    # POST /shutdown blocks until checkpoint + sleep marker are written, then returns.
    # Use the venv Python to make the request -- urllib bypasses system proxy for localhost.
    Write-Host "Flushing JARVIS memory (checkpoint + sleep marker)..." -ForegroundColor Cyan
    $wsPort = if ($env:WS_PORT) { $env:WS_PORT } else { "8765" }
    $python = Join-Path $Root ".venv\Scripts\python.exe"

    if (Test-Path $python) {
        $flushScript = "import urllib.request, sys; r=urllib.request.urlopen('http://127.0.0.1:$wsPort/shutdown', data=b'', timeout=20); sys.exit(0 if r.status==200 else 1)"
        try {
            & $python -c $flushScript 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Memory flushed." -ForegroundColor Green
            } else {
                Write-Host "Memory flush returned non-200 - proceeding." -ForegroundColor Yellow
            }
        } catch {
            Write-Host "Memory flush failed: $_" -ForegroundColor Yellow
        }
    } else {
        Write-Host "Python venv not found - skipping memory flush." -ForegroundColor Yellow
    }

    # Step 2: force-kill the process tree.
    Write-Host "Stopping backend (PID $BackendPid)..." -ForegroundColor Cyan
    cmd /c "taskkill /F /T /PID $BackendPid >nul 2>&1"
    Write-Host "Backend stopped." -ForegroundColor Green
}

if ($FrontendPid -gt 0) {
    Write-Host "Stopping frontend (PID $FrontendPid)..." -ForegroundColor Cyan
    cmd /c "taskkill /F /T /PID $FrontendPid >nul 2>&1"
    Write-Host "Frontend stopped." -ForegroundColor Green
}

# Back up the vault + every Qdrant collection while Qdrant is still running.
# Must happen before "docker compose stop" below -- snapshots need a live
# server. Runs as its own process (not folded into the /shutdown HTTP call
# above) so a slow or failed backup can never block or corrupt the memory
# flush that already completed, and can't get caught by that call's timeout.
Write-Host "Backing up vault + memory..." -ForegroundColor Cyan
$python = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $python) {
    Push-Location $Root
    try {
        & $python -m backend.tools.backup
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Backup complete." -ForegroundColor Green
        } else {
            Write-Host "Backup finished with warnings (see output above)." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "Backup failed: $_" -ForegroundColor Yellow
    } finally {
        Pop-Location
    }
} else {
    Write-Host "Python venv not found - skipping backup." -ForegroundColor Yellow
}

# Stop Qdrant -- use STOP, never DOWN.
# docker compose down -v would delete the qdrant_data volume and wipe all memories.
$composeFile = Join-Path $Root "mem0\docker-compose.yml"
Write-Host "Stopping Qdrant (memories preserved on disk)..." -ForegroundColor Cyan
docker compose -f $composeFile stop
Write-Host "JARVIS stopped. Memory saved." -ForegroundColor Green
