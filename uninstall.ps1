# Removes what install.ps1 added: the JARVIS shortcuts (Start Menu, Desktop),
# the Start with Windows entry (HKCU Run key, and a Startup-folder shortcut if
# one was made by hand). With -RemoveFiles it also deletes the installed Body
# in %LOCALAPPDATA%\Programs\JARVIS. The repo, the venv, your data and the logs
# in %LOCALAPPDATA%\JARVIS are left alone.
#
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -RemoveFiles
#
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 without a BOM.

param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\JARVIS"),
    [switch]$RemoveFiles
)

$ErrorActionPreference = "Stop"
$exe = Join-Path $InstallDir "Jarvis.Body.exe"

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

function Remove-IfThere([string]$path) {
    if (Test-Path $path) {
        Remove-Item -Force -Path $path
        Write-Host "Removed $path"
    }
}

Remove-IfThere (Join-Path ([Environment]::GetFolderPath("Programs")) "JARVIS.lnk")
Remove-IfThere (Join-Path ([Environment]::GetFolderPath("Desktop")) "JARVIS.lnk")
Remove-IfThere (Join-Path ([Environment]::GetFolderPath("Startup")) "JARVIS.lnk")

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if ($null -ne (Get-ItemProperty -Path $runKey -Name "JARVIS" -ErrorAction SilentlyContinue)) {
    Remove-ItemProperty -Path $runKey -Name "JARVIS"
    Write-Host "Removed the Start with Windows entry (HKCU Run\JARVIS)"
}

if ($RemoveFiles -and (Test-Path $InstallDir)) {
    if (-not (Stop-InstalledBody)) {
        Write-Host "JARVIS is still running. Quit it from the tray icon, then run uninstall.ps1 -RemoveFiles again." -ForegroundColor Red
        exit 1
    }
    Remove-Item -Recurse -Force -Path $InstallDir
    Write-Host "Removed $InstallDir"
}

Write-Host "JARVIS is uninstalled." -ForegroundColor Green
if (-not $RemoveFiles) { Write-Host "The Body is still in $InstallDir (use -RemoveFiles to delete it)." }
