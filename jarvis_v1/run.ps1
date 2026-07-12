<#
.SYNOPSIS
    Launch JARVIS with preflight checks.

.EXAMPLE
    .\run.ps1                 # V3 (vault brain), voice mode
    .\run.ps1 -Text           # V3, keyboard mode (no microphone needed)
    .\run.ps1 -V2             # V2 (stable brain)
    .\run.ps1 -Test           # run the test suite instead of launching
    .\run.ps1 -VoiceTest      # A/B test TTS voice/rate/pitch (see tools/voice_test.py)
#>
[CmdletBinding()]
param(
    [switch]$V2,        # run the stable V2 brain (python main.py) instead of V3
    [switch]$Text,      # keyboard input instead of voice
    [switch]$Test,      # run pytest and exit
    [switch]$VoiceTest  # run tools/voice_test.py and exit
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Info($m)  { Write-Host "[ i ] $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "[ok ] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "[warn] $m" -ForegroundColor Yellow }

# -- Test mode ------------------------------------------------------------
if ($Test) {
    Info "Running test suite..."
    python -m pytest tests/ -q
    exit $LASTEXITCODE
}

if ($VoiceTest) {
    Info "Launching voice A/B tester (see tools/voice_test.py --help for options)..."
    python -m tools.voice_test
    exit $LASTEXITCODE
}

# -- Preflight -------------------------------------------------------------
Info "Preflight checks..."

# Python
try { $py = (python --version) 2>&1; Ok "Python: $py" }
catch { Warn "Python not found on PATH."; exit 1 }

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
    python main.py
} else {
    Info "Launching V3 (vault brain) -- Ctrl+C to stop"
    python -m core_v3
}
