#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
if [[ "$CONTROL_ROOT" == "$HOME/project/"* ]]; then
  ROOT="$HOME/project"
else
  ROOT="$CONTROL_ROOT"
fi
PIN_FILE="$CONTROL_ROOT/ci/bare/source-pin.txt"
BARE_ADAPTER="$CONTROL_ROOT/ci/bare/prepare_source.sh"
SISO_ADAPTER="$CONTROL_ROOT/ci/bare/configure_siso.py"
IDENTITY_TOOL="$CONTROL_ROOT/ci/bare/build_identity.py"
MODE="${1:-inspect}"

read_pin() {
  local key="$1"
  sed -n "s/^$key=//p" "$PIN_FILE"
}

CHROMIUM_COMMIT="$(read_pin chromium_commit)"
CHROMIUM_VERSION="$(read_pin chromium_version)"
test -n "$CHROMIUM_COMMIT"
test -n "$CHROMIUM_VERSION"

case "$MODE" in
  inspect|--inspect)
    echo "SANDIUM_BOOTSTRAP_MODE=INSPECT_ONLY"
    echo "SANDIUM_CHROMIUM_COMMIT=$CHROMIUM_COMMIT"
    echo "SANDIUM_CHROMIUM_VERSION=$CHROMIUM_VERSION"
    echo "CHROMIUM_SYNC_RUN=no"
    echo "GN_GENERATION_RUN=no"
    echo "COMPILATION_RUN=no"
    exit 0
    ;;
  bootstrap|--bootstrap)
    ;;
  *)
    echo "Usage: $0 [inspect|bootstrap]" >&2
    exit 2
    ;;
esac

# Full source sync is expensive. It is impossible unless a separate explicit
# opt-in is supplied; the CircleCI workflow parameter alone does not set this.
if [[ "${SANDIUM_ALLOW_CHROMIUM_SYNC:-false}" != "true" ]]; then
  echo "SANDIUM_BOOTSTRAP_BLOCKED=Set SANDIUM_ALLOW_CHROMIUM_SYNC=true only after separate approval." >&2
  exit 30
fi
: "${BUILDBUDDY_API_KEY:?Existing protected BuildBuddy credential is unavailable; stop without creating a replacement}"

DEPOT_TOOLS="$HOME/depot_tools"
if [[ ! -x "$DEPOT_TOOLS/fetch" || ! -x "$DEPOT_TOOLS/gclient" || ! -x "$DEPOT_TOOLS/autoninja" ]]; then
  echo "SANDIUM_DEPOT_TOOLS=NOT_READY"
  echo "This bootstrap requires the existing or preinstalled depot_tools directory."
  echo "Do not clone or update toolchain automatically during source migration."
  exit 31
fi
export PATH="$DEPOT_TOOLS:$PATH"

bash "$BARE_ADAPTER" inspect
python3 "$CONTROL_ROOT/ci/bare/write_cache_identity.py"

if [[ ! -d "$ROOT/src/.git" ]]; then
  if [[ -e "$ROOT/.gclient" || -e "$ROOT/src" ]]; then
    echo "SANDIUM_CHROMIUM_WORKSPACE_CONFLICT=true; refusing to overwrite existing source state." >&2
    exit 32
  fi
  cd "$ROOT"
  "$DEPOT_TOOLS/fetch" --nohooks android
fi

test -f "$ROOT/.gclient"
python3 "$SISO_ADAPTER" --workspace "$ROOT" --write

cd "$ROOT"
"$DEPOT_TOOLS/gclient" sync --nohooks --revision "src@$CHROMIUM_COMMIT"
actual="$(git -C "$ROOT/src" rev-parse HEAD)"
if [[ "$actual" != "$CHROMIUM_COMMIT" ]]; then
  echo "SANDIUM_CHROMIUM_PIN_MISMATCH expected=$CHROMIUM_COMMIT actual=$actual" >&2
  exit 33
fi

bash "$BARE_ADAPTER" apply
"$DEPOT_TOOLS/gclient" runhooks

test -x "$ROOT/src/build/install-build-deps.sh"
sudo "$ROOT/src/build/install-build-deps.sh" --android

GN="$ROOT/src/buildtools/linux64/gn"
test -x "$GN"
GN_ARGS_FILE="$CONTROL_ROOT/ci/bare/gn_args.gn"
test -s "$GN_ARGS_FILE"
"$GN" gen "$ROOT/src/out/android_Debug_arm64" --args="$(cat "$GN_ARGS_FILE")"

python3 "$CONTROL_ROOT/ci/bare/write_cache_identity.py"
python3 "$IDENTITY_TOOL" --mode record --root "$ROOT" --output "$ROOT/sandium-output"
echo "SANDIUM_CHROMIUM_SOURCE_PREPARATION=PASS"
echo "SANDIUM_GN_GENERATION=PASS"
echo "COMPILATION_RUN=no"
echo "APK_BUILD_RUN=no"
