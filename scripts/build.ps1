# Build the JARVIS frontend for production
# Run this once before using: .\scripts\run.ps1 -Prod
$ErrorActionPreference = "Continue"
$root     = Split-Path $PSScriptRoot -Parent
$frontend = Join-Path $root "frontend"

Write-Host ""
Write-Host "=== Building JARVIS frontend ===" -ForegroundColor Cyan

if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Host "ERROR: node_modules not found. Run .\scripts\setup.ps1 first." -ForegroundColor Red
    exit 1
}

Push-Location $frontend
npm run build
$buildExit = $LASTEXITCODE
Pop-Location

if ($buildExit -ne 0) {
    Write-Host "ERROR: Build failed. Check errors above." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Build complete. Run with: .\scripts\run.ps1 -Prod" -ForegroundColor Green
