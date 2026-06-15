# Reggia desktop build - Windows.
#
# Prerequisites (one-time):
#   winget install Rustlang.Rustup
#   rustup default stable-x86_64-pc-windows-msvc
#   cargo install tauri-cli --version "^1.6"
#   winget install astral-sh.uv
#   winget install Microsoft.VisualStudio.2022.BuildTools `
#     --override "--quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
#   # Install WiX 3.x for MSI bundling: https://github.com/wixtoolset/wix3/releases
#
# Usage (from PowerShell, in desktop/):
#   .\build.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$BinariesDir = "src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $BinariesDir | Out-Null

Write-Host "=== Step 1: prepare bundled backend ===" -ForegroundColor Cyan
uv run --with libcst python scripts\prepare_backend.py

Write-Host ""
Write-Host "=== Step 2: PyInstaller ===" -ForegroundColor Cyan
if (Test-Path "build\pyi-win") { Remove-Item -Recurse -Force "build\pyi-win" }
if (Test-Path "dist\win") { Remove-Item -Recurse -Force "dist\win" }

uv run --with pyinstaller --with libcst `
  pyinstaller scripts\reggia_launcher.spec `
  --noconfirm `
  --distpath "dist\win" `
  --workpath "build\pyi-win"

$pyiExe = "dist\win\reggia-backend\reggia-backend.exe"
if (-not (Test-Path $pyiExe)) {
  Write-Error "PyInstaller did not produce $pyiExe"
  exit 1
}

# Tauri sidecar suffix for Windows
$target = "x86_64-pc-windows-msvc"
$outName = "reggia-backend-$target.exe"
Copy-Item -Force $pyiExe "$BinariesDir\$outName"
Write-Host "  staged: $BinariesDir\$outName"

Write-Host ""
Write-Host "=== Step 3: tauri build ===" -ForegroundColor Cyan
Set-Location src-tauri
cargo tauri build

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Artifacts in: src-tauri\target\release\bundle\msi\"
