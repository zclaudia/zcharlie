#!/usr/bin/env bash
# Generate platform resources from the approved v2 brand assets.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# Keep generation separate from gen/android so a fresh checkout gives the same assets.
ICON_TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$ICON_TMP_DIR"' EXIT
pnpm exec tauri icon src-tauri/icons/sources/manifest.json --output "$ICON_TMP_DIR/icons"
mkdir -p src-tauri/icons/android
cp -R "$ICON_TMP_DIR/icons/android/." src-tauri/icons/android/
cp "$ICON_TMP_DIR/icons/icon.png" src-tauri/icons/icon.png
# Round launchers must use the same adaptive layers on Android 8+.
cp src-tauri/icons/android/mipmap-anydpi-v26/ic_launcher.xml \
  src-tauri/icons/android/mipmap-anydpi-v26/ic_launcher_round.xml

if [[ -d src-tauri/gen/android/app/src/main/res ]]; then
  cp -R src-tauri/icons/android/. src-tauri/gen/android/app/src/main/res/
fi
