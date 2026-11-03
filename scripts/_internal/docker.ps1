# Internal -- do not run directly. Called by setup.ps1 and run.ps1.
# Starts the Qdrant vector database and SearXNG (web search), waits for health.
param(
    [Parameter(Mandatory)][string]$Root,
    [ValidateSet("setup", "run")][string]$Mode = "run"
)
$ErrorActionPreference = "Continue"

$composeFile = Join-Path $Root "mem0\docker-compose.yml"
# 20s (run mode's old value) was measured too tight live (2026-08-27): a cold
# container start on Windows/Docker Desktop -- competing for disk/CPU with
# SearXNG starting at the same moment and the Python backend's own startup
# right after -- routinely took longer than that even though Qdrant's own
# data volume was tiny (29MB, not a "big collection to reload" issue). The
# script only warns and degrades gracefully either way, but a timeout that's
# reliably too short makes the warning fire on essentially every normal
# start, which defeats the point of having a real signal at all.
# UPDATE (2026-08-28): the 40s bump alone didn't fix it -- the actual root cause
# was that every check below used the hostname "localhost", not real slowness. See
# the note above the first Invoke-WebRequest call for the real fix.
$timeout     = if ($Mode -eq "setup") { 40 } else { 40 }
$interval    = if ($Mode -eq "setup") { 3  } else { 2  }

# Check current health (compose up -d is idempotent, but we can skip waits)
# NOTE: use 127.0.0.1, never "localhost" -- Qdrant/SearXNG are bound loopback-only
# (127.0.0.1:PORT in docker-compose.yml, so a public server never exposes them), and on
# this machine "localhost" resolves AAAA (::1) before A (127.0.0.1). Nothing listens
# on ::1, but the connection doesn't fail fast -- it hangs for the full -TimeoutSec
# every single time. Confirmed live (2026-08-28): "http://localhost:6333/readyz"
# timed out 3/3 attempts at ~2000ms each while "http://127.0.0.1:6333/readyz"
# succeeded in 35ms -- Qdrant was healthy and serving traffic the entire time this
# script was reporting "did not start in 40s". Raising the timeout (done 2026-08-27)
# never could have fixed this: the wait loop was doomed on every iteration regardless
# of how long, because "localhost" never succeeds on this host at all.
$qdrantUp = $false
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:6333/readyz" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
    if ($r.StatusCode -eq 200) { $qdrantUp = $true }
} catch {}

# Always run compose up: it no-ops for running services and starts any missing
# one (e.g. Qdrant left running from a prior session but SearXNG stopped).
if ($qdrantUp) {
    Write-Host "Qdrant already running -- ensuring SearXNG is up too..." -ForegroundColor Green
} else {
    Write-Host "Starting Qdrant + SearXNG..." -ForegroundColor Cyan
}
docker compose -f $composeFile up -d
if ($LASTEXITCODE -ne 0) {
    if ($qdrantUp) {
        Write-Host "WARNING: docker compose up failed -- SearXNG may be unavailable (search will use fallbacks)." -ForegroundColor Yellow
    } else {
        throw "docker compose up failed (exit $LASTEXITCODE). See output above for details."
    }
}

if (-not $qdrantUp) {
    Write-Host "Waiting for Qdrant to be ready (up to ${timeout}s)..." -ForegroundColor Cyan
    $waited = 0
    $ready  = $false
    while ($waited -lt $timeout) {
        Start-Sleep -Seconds $interval
        $waited += $interval
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:6333/readyz" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
    }

    if ($ready) {
        Write-Host "Qdrant ready." -ForegroundColor Green
    } elseif ($Mode -eq "setup") {
        throw "Qdrant did not become ready in ${timeout}s. Run setup.ps1 again once Docker finishes pulling the image."
    } else {
        # In run mode, warn but don't block -- memory.py degrades gracefully if Qdrant is unreachable
        Write-Host "WARNING: Qdrant did not start in ${timeout}s -- memory features will be degraded." -ForegroundColor Yellow
    }
}

# SearXNG health -- non-blocking: web_browse degrades to keyless fallbacks if it's down.
$searxReady = $false
$waited = 0
while ($waited -lt 10) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8088/healthz" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($r.StatusCode -eq 200) { $searxReady = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
    $waited += 2
}
if ($searxReady) {
    Write-Host "SearXNG ready (web search)." -ForegroundColor Green
} else {
    Write-Host "WARNING: SearXNG not responding on :8088 -- web search will use keyless fallbacks." -ForegroundColor Yellow
}
