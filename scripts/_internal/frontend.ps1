# Internal -- do not run directly. Runs the frontend in the current window.
param(
    [Parameter(Mandatory)][string]$Root,
    [switch]$Prod   # load from dist/ instead of Vite dev server
)
$ErrorActionPreference = "Continue"

$frontend = Join-Path $Root "frontend"
Set-Location $frontend

if ($Prod) {
    $dist = Join-Path $frontend "dist\index.html"
    if (-not (Test-Path $dist)) {
        Write-Host "ERROR: No production build found. Run .\scripts\build.ps1 first." -ForegroundColor Red
        exit 1
    }
    Write-Host "JARVIS frontend starting (production build)..." -ForegroundColor Cyan
    $env:NODE_ENV = "production"
    npm run electron
} else {
    Write-Host "JARVIS frontend starting (dev mode)..." -ForegroundColor Cyan
    $env:NODE_ENV = "development"
    npm run start
}
