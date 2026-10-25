#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
if [[ "$CONTROL_ROOT" == "$HOME/project/"* ]]; then
  ROOT="$HOME/project"
else
  ROOT="$CONTROL_ROOT"
fi

SOURCE_APK="${CHROMIUM_APK:-$ROOT/src/out/android_Debug_arm64/apks/ChromePublic.apk}"
OUTPUT_DIR="$ROOT/sandium-output"
FINAL_APK="$OUTPUT_DIR/Sandium-debug-arm64.apk"
IDENTITY="$CONTROL_ROOT/ci/bare/build_identity.py"

test -s "$SOURCE_APK" || {
  echo "SANDIUM_APK_SOURCE_MISSING=$SOURCE_APK" >&2
  exit 40
}
mkdir -p "$OUTPUT_DIR"
install -m 0644 "$SOURCE_APK" "$FINAL_APK"
python3 "$IDENTITY" --mode record --root "$ROOT" --apk "$FINAL_APK" --output "$OUTPUT_DIR"
python3 "$IDENTITY" --mode verify --root "$ROOT" --apk "$FINAL_APK" --output "$OUTPUT_DIR"

test -s "$OUTPUT_DIR/build-manifest.json"
sha256sum "$FINAL_APK" > "$OUTPUT_DIR/Sandium-debug-arm64.apk.sha256"
stat -c 'SANDIUM_APK_BYTES=%s' "$FINAL_APK"
sha256sum "$FINAL_APK" | awk '{print "SANDIUM_APK_SHA256="$1}'
echo "SANDIUM_ARTIFACT_PATH=$FINAL_APK"
echo "SANDIUM_ARTIFACT_VERIFICATION=PASS"
