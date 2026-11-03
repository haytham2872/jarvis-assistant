# Internal -- do not run directly. Runs the uvicorn backend in the current window.
param(
    [Parameter(Mandatory)][string]$Root,
    [switch]$NoWake
)

$python = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root

$backendArgs = @("-m", "backend.main")
if ($NoWake) { $backendArgs += "--no-wake" }

Write-Host "JARVIS backend starting..." -ForegroundColor Cyan
& $python @backendArgs
