# Build Media Browser for Windows (zip distribution)
# Requires: Python 3.12+, ffmpeg.exe and ffprobe.exe in PATH
# Usage: powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
$VERSION = "1.0.13"

# Check for ffmpeg
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
$ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
if (-not $ffmpeg -or -not $ffprobe) {
    Write-Error "ffmpeg and/or ffprobe not found in PATH. Please install ffmpeg first."
    exit 1
}

Write-Host "==> Install PyInstaller"
python -m pip install --upgrade pip
python -m pip install "pyinstaller>=6.0"

Write-Host "==> Build with PyInstaller"
Push-Location $ROOT
python -m PyInstaller packaging\MediaBrowser.spec --clean -y
Pop-Location

$DIST = Join-Path $ROOT "dist" "Media Browser"
if (-not (Test-Path $DIST)) {
    Write-Error "Build failed: $DIST not found"
    exit 1
}

Write-Host "==> Create ZIP archive"
$ZIP_NAME = "MediaBrowser-v${VERSION}-windows.zip"
$ZIP_OUT = Join-Path $ROOT "dist" $ZIP_NAME

# Compress using PowerShell 5.1+ / 7+
if (Get-Command Compress-Archive -ErrorAction SilentlyContinue) {
    Compress-Archive -Path "$DIST\*" -DestinationPath $ZIP_OUT -Force
} else {
    # Fallback for older PowerShell
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory($DIST, $ZIP_OUT, "Optimal", $false)
}

Write-Host "Done:"
Write-Host "  $ZIP_OUT"
