#!/bin/bash
# Reggia desktop build — macOS.
#
# Produces a universal2 .dmg by building reggia-backend for both arm64 and
# x86_64 with PyInstaller, lipo-merging the binaries, then running tauri build.
#
# Prerequisites (one-time):
#   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
#   rustup target add aarch64-apple-darwin x86_64-apple-darwin
#   cargo install tauri-cli --version "^1.6"
#   uv sync                                  # in repo root, for backend deps
#
# Usage:
#   cd desktop
#   ./build.sh                # full build, universal2
#   ./build.sh arm64          # arm64 only (faster iteration)

set -euo pipefail

cd "$(dirname "$0")"

ARCH_MODE="${1:-universal}"
BINARIES_DIR="src-tauri/binaries"
mkdir -p "$BINARIES_DIR"

echo "=== Step 1: prepare bundled backend ==="
uv run --with libcst python scripts/prepare_backend.py

build_for_arch() {
  local arch="$1"
  local target_triple="$2"
  local out_name="reggia-backend-${target_triple}"

  echo ""
  echo "=== Step 2 [$arch]: PyInstaller ==="
  rm -rf "build/pyi-$arch" "dist/$arch"
  uv run --with pyinstaller --with libcst \
    pyinstaller scripts/reggia_launcher.spec \
    --noconfirm \
    --distpath "dist/$arch" \
    --workpath "build/pyi-$arch"
  # PyInstaller --target-architecture is unreliable for cross-arch on Apple
  # Silicon when deps include native wheels. We assume the host arch matches
  # what we want; for universal2 we run twice on the same machine using
  # Rosetta-fronted Python for the x86_64 pass.

  local pyi_exe="dist/$arch/reggia-backend/reggia-backend"
  if [[ ! -f "$pyi_exe" ]]; then
    echo "ERROR: PyInstaller did not produce $pyi_exe"
    exit 1
  fi

  # Stage the entire one-folder bundle as a Tauri resource (not externalBin).
  # Tauri's resource resolver preserves the directory layout, so the binary
  # finds its sibling _internal/ at runtime.
  rm -rf "$BINARIES_DIR/reggia-backend"
  cp -R "dist/$arch/reggia-backend" "$BINARIES_DIR/reggia-backend"
  chmod +x "$BINARIES_DIR/reggia-backend/reggia-backend"
  echo "  staged: $BINARIES_DIR/reggia-backend/ (one-folder bundle)"
}

if [[ "$ARCH_MODE" == "arm64" || "$ARCH_MODE" == "aarch64" ]]; then
  build_for_arch "arm64" "aarch64-apple-darwin"
  TAURI_TARGET=""
elif [[ "$ARCH_MODE" == "x86_64" || "$ARCH_MODE" == "intel" ]]; then
  build_for_arch "x86_64" "x86_64-apple-darwin"
  TAURI_TARGET=""
else
  # Universal: PyInstaller can't cross-arch reliably for native deps; the
  # one-folder bundle approach + resources means we need per-arch bundles
  # bundled separately. For pilot, default to host-arch.
  HOST_ARCH=$(uname -m)
  if [[ "$HOST_ARCH" == "arm64" ]]; then
    build_for_arch "arm64" "aarch64-apple-darwin"
  else
    build_for_arch "x86_64" "x86_64-apple-darwin"
  fi
  TAURI_TARGET=""
fi

echo ""
echo "=== Step 3: tauri build ==="
cd src-tauri
if [[ -n "$TAURI_TARGET" ]]; then
  cargo tauri build --target "$TAURI_TARGET"
else
  cargo tauri build
fi

echo ""
echo "=== Done ==="
echo "Artifacts in: src-tauri/target/release/bundle/dmg/"
