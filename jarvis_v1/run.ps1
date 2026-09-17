<#
.SYNOPSIS
    Launch JARVIS with preflight checks.

.EXAMPLE
    .\run.ps1                 # V3 (vault brain), voice mode
    .\run.ps1 -Text           # V3, keyboard mode (no microphone needed)
    .\run.ps1 -V2             # V2 (stable brain)
    .\run.ps1 -Test           # run the test suite instead of launching
    .\run.ps1 -VoiceTest      # A/B test TTS voice/rate/pitch (see tools/voice_test.py)
    .\run.ps1 -MicCheck       # diagnose mic / STT capture (see tools/mic_check.py)
#>
[CmdletBinding()]
param(
    [switch]$V2,        # run the stable V2 brain (python main.py) instead of V3
    [switch]$Text,      # keyboard input instead of voice
    [switch]$Test,      # run pytest and exit
    [switch]$VoiceTest, # run tools/voice_test.py and exit
    [switch]$MicCheck,  # run tools/mic_check.py and exit
    [switch]$MicSmoke,  # run tools/mic_smoke.py (V3 voice end-to-end) and exit
    [switch]$WakeCheck  # run tools/wake_check.py (wake-word score meter) and exit
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Info($m)  { Write-Host "[ i ] $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "[ok ] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "[warn] $m" -ForegroundColor Yellow }

# Use the project venv's interpreter if present. It has the voice dependencies
# (openWakeWord, Silero VAD + torch, Piper). Bare `python` is usually the SYSTEM
# Python, which lacks them and silently disables wake detection + neural VAD
# (JARVIS then listens continuously and mis-transcribes ambient noise).
$Py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python"; Warn "venv not found - using system 'python' (voice deps may be missing)." }

# -- Test mode ------------------------------------------------------------
if ($Test) {
    Info "Running test suite..."
    & $Py -m pytest tests/ -q
    exit $LASTEXITCODE
}

if ($VoiceTest) {
    Info "Launching voice A/B tester (see tools/voice_test.py --help for options)..."
    & $Py -m tools.voice_test
    exit $LASTEXITCODE
}

if ($MicCheck) {
    Info "Launching mic / STT diagnostic (see tools/mic_check.py)..."
    & $Py -m tools.mic_check
    exit $LASTEXITCODE
}

if ($MicSmoke) {
    Info "Launching V3 voice end-to-end smoke test (see tools/mic_smoke.py)..."
    & $Py -m tools.mic_smoke
    exit $LASTEXITCODE
}

if ($WakeCheck) {
    Info "Launching wake-word score meter (see tools/wake_check.py)..."
    & $Py -m tools.wake_check
    exit $LASTEXITCODE
}

# -- Preflight -------------------------------------------------------------
Info "Preflight checks..."

# Python
try { $pyver = (& $Py --version) 2>&1; Ok "Python: $pyver ($Py)" }
catch { Warn "Python not found."; exit 1 }

# Voice dependencies - warn clearly if missing (wake word + neural VAD need them).
$voiceMissing = (& $Py -c "import importlib.util as u; print(','.join(m for m in ('openwakeword','silero_vad','piper') if u.find_spec(m) is None))") 2>&1
if ($voiceMissing) {
    Warn "Missing voice deps: $voiceMissing"
    Warn "  Install with:  $Py -m pip install -e `".[voice]`""
    Warn "  Without them: wake word disabled + RMS-only VAD (degraded voice UX)."
} else {
    Ok "Voice deps present (openWakeWord, Silero, Piper)."
}

# Ollama reachable?
$ollamaOk = $false
try {
    $tags = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3
    $models = $tags.models.name
    $ollamaOk = $true
    Ok "Ollama is up ($($models.Count) model(s))."
} catch {
    Warn "Ollama not reachable at localhost:11434."
    Warn "  Start it with:  ollama serve"
    Warn "  JARVIS will still run, but LLM replies and semantic recall will be unavailable."
}

# Required models
if ($ollamaOk) {
    foreach ($m in @("qwen2.5", "nomic-embed-text")) {
        if ($models -match $m) { Ok "Model present: $m" }
        else { Warn "Model missing: $m  ->  ollama pull $m" }
    }
}

# -- Mode / target ---------------------------------------------------------
# Default (no flag): let config decide (wake mode). Only override for -Text.
if ($Text) { $env:JARVIS_MODE = "text"; Info "Mode: text (keyboard)" }
else       { Remove-Item Env:\JARVIS_MODE -ErrorAction SilentlyContinue; Info "Mode: wake (say 'jarvis')" }

Write-Host ""
if ($V2) {
    Info "Launching V2 (core_v2 brain) -- Ctrl+C to stop"
    & $Py main.py
} else {
    Info "Launching V3 (vault brain) -- Ctrl+C to stop"
    & $Py -m core_v3
}
