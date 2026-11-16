# Installs JARVIS for this Windows user (no admin rights):
#   - publishes the Body (HUD, tray, launcher) as one exe into
#     %LOCALAPPDATA%\Programs\JARVIS (framework-dependent: needs the
#     .NET 8+ Desktop Runtime),
#   - points it at this repo's jarvis_v1 folder (body.settings.json),
#   - creates "JARVIS" shortcuts in the Start Menu and on the Desktop,
#   - with -StartWithWindows, starts it at sign-in (HKCU Run key).
#
# Double-clicking the shortcut then starts everything: the Body checks for a
# running Brain, otherwise checks Ollama and the models, starts Ollama if
# needed, and starts the Brain hidden. Quit from the tray stops what it started.
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -StartWithWindows -Launch
#
# Run it again after pulling changes to update the installed Body. Undo with
# uninstall.ps1. ASCII only: Windows PowerShell 5.1 misreads UTF-8 without a BOM.

param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\JARVIS"),
    [switch]$StartWithWindows,
    [switch]$NoDesktopShortcut,
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$jarvisRoot = Join-Path $repo "jarvis_v1"
$project = Join-Path $repo "body\src\Jarvis.Body\Jarvis.Body.csproj"
$exe = Join-Path $InstallDir "Jarvis.Body.exe"

function Fail([string]$message) {
    Write-Host ""
    Write-Host "JARVIS was not installed: $message" -ForegroundColor Red
    exit 1
}

function Get-InstalledBody {
    @(Get-Process -Name "Jarvis.Body" -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and ([IO.Path]::GetFullPath($_.Path) -ieq [IO.Path]::GetFullPath($exe)) } catch { $false }
    })
}

# Ask a running Body to quit the way the tray's Quit does (it stops the Brain,
# and Ollama/PhoneBox if it started them). True once it has exited.
function Stop-InstalledBody {
    if ((Get-InstalledBody).Count -eq 0) { return $true }
    Write-Host "==> JARVIS is running; asking it to quit (Jarvis.Body.exe --quit)" -ForegroundColor Cyan
    Start-Process -FilePath $exe -ArgumentList "--quit" -Wait
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ((Get-InstalledBody).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Step([string]$message) { Write-Host "==> $message" -ForegroundColor Cyan }

# -- Checks -------------------------------------------------------------------

if (-not (Test-Path (Join-Path $jarvisRoot "core_v3\__main__.py"))) {
    Fail "$jarvisRoot doesn't look like jarvis_v1 (no core_v3\__main__.py). Run install.ps1 from the JARVIS repo."
}
if (-not (Test-Path (Join-Path $jarvisRoot "venv\Scripts\python.exe"))) {
    Write-Host "Note: there is no venv in $jarvisRoot yet. Create it (see README) before starting JARVIS," -ForegroundColor Yellow
    Write-Host "      or set python_path in $InstallDir\body.settings.json." -ForegroundColor Yellow
}

$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) {
    Fail "the .NET SDK isn't installed (dotnet is not on PATH). Install the .NET 8 SDK or newer from https://dotnet.microsoft.com/download"
}
$sdks = @(& dotnet --list-sdks 2>$null | Where-Object { $_ -match '^(\d+)\.' -and [int]$Matches[1] -ge 8 })
if ($sdks.Count -eq 0) {
    Fail "building the Body needs the .NET 8 SDK or newer (dotnet --list-sdks shows none)."
}
$desktop = @(& dotnet --list-runtimes 2>$null | Where-Object { $_ -match '^Microsoft\.WindowsDesktop\.App (\d+)\.' -and [int]$Matches[1] -ge 8 })
if ($desktop.Count -eq 0) {
    Fail "the .NET Desktop Runtime 8 or newer isn't installed (needed to run the Body)."
}

if (-not (Stop-InstalledBody)) {
    Fail "the installed JARVIS is still running. Quit it from the tray icon (right-click > Quit), then run install.ps1 again."
}

# -- Publish --------------------------------------------------------------------

Step "Publishing the Body to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
& dotnet publish $project -c Release -p:PublishSingleFile=true -p:DebugType=embedded -nodeReuse:false -o $InstallDir --nologo -v quiet
if ($LASTEXITCODE -ne 0) { Fail "dotnet publish failed (exit $LASTEXITCODE). Build it once with: dotnet build body\Jarvis.Body.sln" }
if (-not (Test-Path $exe)) { Fail "publish finished but $exe is missing." }
Get-ChildItem -Path $InstallDir -Filter "*.pdb" -ErrorAction SilentlyContinue | Remove-Item -Force

# -- Settings: keep what's there, set jarvis_root if it isn't valid -------------

$settingsPath = Join-Path $InstallDir "body.settings.json"
$settings = $null
if (Test-Path $settingsPath) {
    try { $settings = Get-Content -Raw -Path $settingsPath | ConvertFrom-Json } catch { $settings = $null }
    if ($null -eq $settings) {
        Copy-Item $settingsPath "$settingsPath.bad" -Force
        Write-Host "Note: body.settings.json couldn't be read; kept a copy as body.settings.json.bad" -ForegroundColor Yellow
    }
}
if ($null -eq $settings) { $settings = New-Object PSObject }
$current = $settings.PSObject.Properties["jarvis_root"]
if ($null -eq $current -or -not (Test-Path (Join-Path ([string]$current.Value) "core_v3\__main__.py"))) {
    $settings | Add-Member -NotePropertyName "jarvis_root" -NotePropertyValue $jarvisRoot -Force
    $json = $settings | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($settingsPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Step "Set jarvis_root = $jarvisRoot"
} else {
    Step "Kept jarvis_root = $($current.Value)"
}

# -- Shortcuts --------------------------------------------------------------------

$shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$path) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $exe
    $link.WorkingDirectory = $InstallDir
    $link.IconLocation = "$exe,0"
    $link.Description = "JARVIS: starts the assistant (HUD, Brain, Ollama)"
    $link.Save()
    Step "Shortcut: $path"
}
$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "JARVIS.lnk"
New-Shortcut $startMenu
if (-not $NoDesktopShortcut) {
    New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "JARVIS.lnk")
}

# -- Start with Windows -------------------------------------------------------

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if ($StartWithWindows) {
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name "JARVIS" -Value "`"$exe`""
    Step "Start with Windows: on (HKCU Run key; toggle it from the tray menu)"
} else {
    $existing = (Get-ItemProperty -Path $runKey -Name "JARVIS" -ErrorAction SilentlyContinue).JARVIS
    if ($existing) { Step "Start with Windows: left on (already set)" }
}

Write-Host ""
Write-Host "JARVIS is installed. Start it from the Start Menu or the Desktop shortcut." -ForegroundColor Green
Write-Host "Logs: $env:LOCALAPPDATA\JARVIS (body.log, brain.log, ollama.log); tray menu > Open logs folder."
if ($Launch) {
    Start-Process -FilePath $exe -WorkingDirectory $InstallDir
}
