# JARVIS first-time setup -- safe to re-run
$ErrorActionPreference = "Continue"
$root        = Split-Path $PSScriptRoot -Parent
$internalDir = Join-Path $PSScriptRoot "_internal"

Write-Host ""
Write-Host "=== JARVIS Setup ===" -ForegroundColor Cyan

# --- Prerequisites -----------------------------------------------------------

# Python (>= 3.10)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Python not found. Install Python 3.10+ from https://python.org" -ForegroundColor Red
    exit 1
}
$pyVer = python --version 2>&1
Write-Host "Python  : $pyVer" -ForegroundColor Green

# Node.js (>= 18)
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Node.js not found. Install Node.js 18+ from https://nodejs.org" -ForegroundColor Red
    exit 1
}
Write-Host "Node    : $(node --version)" -ForegroundColor Green

# Docker Desktop
cmd /c "docker info >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker Desktop is not running." -ForegroundColor Red
    Write-Host "       Start Docker Desktop and re-run setup.ps1." -ForegroundColor Yellow
    exit 1
}
Write-Host "Docker  : running" -ForegroundColor Green

# winget (used for espeak-ng; optional)
$hasWinget = $null -ne (Get-Command winget -ErrorAction SilentlyContinue)

# --- .env --------------------------------------------------------------------
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host ".env created -- fill in OPENROUTER_API_KEY before running JARVIS (docs/SETUP.md)." -ForegroundColor Yellow
}

# --- Python environment ------------------------------------------------------
$venv = Join-Path $root ".venv"
if (-not (Test-Path $venv)) {
    Write-Host ""
    Write-Host "Creating Python venv..." -ForegroundColor Cyan
    python -m venv $venv
}
$pip    = Join-Path $venv "Scripts\pip.exe"
$python = Join-Path $venv "Scripts\python.exe"

Write-Host "Upgrading pip..." -ForegroundColor Cyan
& $python -m pip install --upgrade pip -q

Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
& $pip install -r (Join-Path $root "requirements.txt") -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed. Check errors above." -ForegroundColor Red
    exit 1
}
Write-Host "Python dependencies installed." -ForegroundColor Green

# --- espeak-ng (required by Kokoro TTS fallback) -----------------------------
Write-Host ""
Write-Host "Checking espeak-ng..." -ForegroundColor Cyan
$espeakOk = $false
try { espeak-ng --version 2>&1 | Out-Null; $espeakOk = ($LASTEXITCODE -eq 0) } catch {}

if ($espeakOk) {
    Write-Host "espeak-ng already installed." -ForegroundColor Green
} elseif ($hasWinget) {
    Write-Host "Installing espeak-ng via winget..." -ForegroundColor Cyan
    winget install espeak-ng --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "espeak-ng installed." -ForegroundColor Green
    } else {
        Write-Host "WARNING: espeak-ng install failed. Install manually: https://github.com/espeak-ng/espeak-ng/releases" -ForegroundColor Yellow
    }
} else {
    Write-Host "WARNING: winget not available. Install espeak-ng manually: https://github.com/espeak-ng/espeak-ng/releases" -ForegroundColor Yellow
}

# --- openWakeWord models -----------------------------------------------------
Write-Host ""
Write-Host "Downloading openWakeWord models..." -ForegroundColor Cyan
$owwScript = Join-Path $env:TEMP "jarvis_oww.py"
Set-Content -Path $owwScript -Encoding utf8 -Value @'
import openwakeword
openwakeword.utils.download_models()
print("openWakeWord models ready.")
'@
& $python $owwScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: openWakeWord model download failed -- keyboard mode (-NoWake) still works." -ForegroundColor Yellow
} else {
    Write-Host "openWakeWord models ready." -ForegroundColor Green
}

# --- Whisper model (size read from .env, downloads once) ---------------------
$whisperModel = "base"
$envLines = Get-Content (Join-Path $root ".env") -ErrorAction SilentlyContinue
foreach ($line in $envLines) {
    if ($line -match '^WHISPER_MODEL=([a-zA-Z0-9._-]+)') {
        $whisperModel = $matches[1]; break
    }
}
Write-Host ""
Write-Host "Pre-loading Whisper '$whisperModel' model (downloads once)..." -ForegroundColor Cyan
$warmup = Join-Path $env:TEMP "jarvis_warmup.py"
Set-Content -Path $warmup -Encoding utf8 -Value "from faster_whisper import WhisperModel`nWhisperModel('$whisperModel', device='cpu', compute_type='int8')`nprint('Whisper $whisperModel model ready.')"
& $python $warmup
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: Whisper warmup failed -- model will download on first use." -ForegroundColor Yellow
} else {
    Write-Host "Whisper '$whisperModel' model ready." -ForegroundColor Green
}

# --- Kokoro TTS model (~300 MB, downloads once) ------------------------------
Write-Host ""
Write-Host "Pre-loading Kokoro voice model (~300 MB, downloads once)..." -ForegroundColor Cyan
$kokoroWarmup = Join-Path $env:TEMP "jarvis_kokoro.py"
Set-Content -Path $kokoroWarmup -Encoding utf8 -Value @'
from kokoro import KPipeline
pipeline = KPipeline(lang_code='a')
for _ in pipeline("Hello.", voice='am_adam', speed=1.0):
    pass
print("Kokoro voice model downloaded.")
'@
& $python $kokoroWarmup
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: Kokoro warmup failed -- model will download on first use." -ForegroundColor Yellow
} else {
    Write-Host "Kokoro voice model downloaded." -ForegroundColor Green
}

# --- fastembed BM25 model (for mem0 hybrid search, ~1 MB, downloads once) ----
Write-Host ""
Write-Host "Pre-loading fastembed BM25 model for hybrid memory search (~1 MB, downloads once)..." -ForegroundColor Cyan
$bm25Warmup = Join-Path $env:TEMP "jarvis_bm25.py"
Set-Content -Path $bm25Warmup -Encoding utf8 -Value @'
from fastembed import SparseTextEmbedding
m = SparseTextEmbedding(model_name="Qdrant/bm25")
list(m.embed(["warmup"]))
print("BM25 model ready.")
'@
& $python $bm25Warmup
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: BM25 model download failed -- memory search will use dense vectors only." -ForegroundColor Yellow
} else {
    Write-Host "BM25 model ready." -ForegroundColor Green
}

# --- sentence-transformers embedding model (~90 MB, downloads once) ----------
# thenlper/gte-small (384-dim) is the shared embedder for mem0, the semantic
# index, and the dedup pass — stronger than MiniLM on retrieval, no query prefix.
Write-Host ""
Write-Host "Pre-loading sentence-transformers embedding model (~90 MB, downloads once)..." -ForegroundColor Cyan
$stWarmup = Join-Path $env:TEMP "jarvis_st.py"
Set-Content -Path $stWarmup -Encoding utf8 -Value @'
from sentence_transformers import SentenceTransformer
SentenceTransformer("thenlper/gte-small")
print("Embedding model ready.")
'@
& $python $stWarmup
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: Embedding model download failed -- will download on first use." -ForegroundColor Yellow
} else {
    Write-Host "Embedding model ready." -ForegroundColor Green
}

# --- npm install -------------------------------------------------------------
Write-Host ""
Write-Host "Installing npm dependencies..." -ForegroundColor Cyan
$frontend = Join-Path $root "frontend"
Push-Location $frontend
npm install --silent
$npmExit = $LASTEXITCODE
Pop-Location
if ($npmExit -ne 0) {
    Write-Host "ERROR: npm install failed." -ForegroundColor Red
    exit 1
}
Write-Host "npm dependencies installed." -ForegroundColor Green

# --- Qdrant vector database (memory storage) ---------------------------------
Write-Host ""
Write-Host "Starting Qdrant vector database..." -ForegroundColor Cyan
try {
    & "$internalDir\docker.ps1" -Root $root -Mode setup
} catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    Write-Host "Tip: Run setup.ps1 again once Docker finishes pulling the Qdrant image." -ForegroundColor Yellow
    exit 1
}

# --- Done --------------------------------------------------------------------
Write-Host ""
Write-Host "=== Setup complete! ===" -ForegroundColor Green
Write-Host "Run: .\scripts\run.ps1" -ForegroundColor Yellow
