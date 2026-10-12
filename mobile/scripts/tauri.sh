#!/usr/bin/env bash
# Project-local Tauri wrapper; clean checkouts bootstrap the generated Android project.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"
source "$SCRIPT_DIR/env.sh"

if [[ "${1:-}" == "android" && ( "${2:-}" == "build" || "${2:-}" == "dev" ) ]]; then
  if [[ ! -f src-tauri/gen/android/gradlew ]]; then
    cargo tauri android init
  fi
  # Android merges this overlay into debug APKs only; release remains HTTPS-only.
  mkdir -p src-tauri/gen/android/app/src/debug
  cat > src-tauri/gen/android/app/src/debug/AndroidManifest.xml <<'EOF'
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
  <application android:usesCleartextTraffic="true" />
</manifest>
EOF
fi

exec cargo tauri "$@"
