# Build Media Browser for Windows (zip distribution)
# Requires: Python 3.12+, ffmpeg.exe and ffprobe.exe in PATH
# Usage: powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
$py = Join-Path $ROOT "media_browser.py"
$VERSION = "0.0.0"
if (Test-Path $py) {
    $m = Select-String -Path $py -Pattern 'APP_VERSION = "([^"]+)"' | Select-Object -First 1
    if ($m) { $VERSION = $m.Matches.Groups[1].Value }
}

# Check for ffmpeg
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
$ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
if (-not $ffmpeg -or -not $ffprobe) {
    Write-Error "ffmpeg and/or ffprobe not found in PATH. Please install ffmpeg first."
    exit 1
}
Write-Host "Found ffmpeg: $($ffmpeg.Source)"
Write-Host "Found ffprobe: $($ffprobe.Source)"

Write-Host "==> Install/upgrade PyInstaller"
python -m pip install --upgrade pip
python -m pip install "pyinstaller>=6.0"

Write-Host "==> Build with PyInstaller"
Push-Location $ROOT

try {
    python -m PyInstaller packaging\MediaBrowser.spec --clean -y
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
} catch {
    Write-Error "PyInstaller build failed: $_"
    Pop-Location
    exit 1
}

Pop-Location

$DIST = Join-Path $ROOT "dist" "Media Browser"
if (-not (Test-Path $DIST)) {
    Write-Error "Build failed: dist directory not found at $DIST"
    exit 1
}

$notice = Join-Path $ROOT "packaging" "README-WINDOWS.txt"
if (Test-Path $notice) {
    $noticeOut = Join-Path $DIST "README-WINDOWS.txt"
    (Get-Content $notice -Raw).Replace("VERSION", $VERSION) | Set-Content -Path $noticeOut -Encoding UTF8
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

if (-not (Test-Path $ZIP_OUT)) {
    Write-Error "ZIP creation failed"
    exit 1
}

Write-Host "Done:"
Write-Host "  $ZIP_OUT"
