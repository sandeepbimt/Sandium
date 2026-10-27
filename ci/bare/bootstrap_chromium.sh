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
CACHE_IDENTITY_TOOL="$CONTROL_ROOT/ci/bare/write_cache_identity.py"
MODE="${1:-inspect}"

read_pin() {
  local key="$1"
  sed -n "s/^$key=//p" "$PIN_FILE"
}

CHROMIUM_COMMIT="$(read_pin chromium_commit)"
CHROMIUM_VERSION="$(read_pin chromium_version)"
EXPECTED_BARE="$(read_pin bare_commit)"
test -n "$CHROMIUM_COMMIT"
test -n "$CHROMIUM_VERSION"
test -n "$EXPECTED_BARE"

DEPOT_TOOLS="$HOME/depot_tools"
CHROMIUM_SRC="$ROOT/src"
BARE_DIR="$ROOT/bare-browser"
OUT="$CHROMIUM_SRC/out/android_Debug_arm64"

check_toolchain() {
  if [[ ! -x "$DEPOT_TOOLS/fetch" || ! -x "$DEPOT_TOOLS/gclient" || ! -x "$DEPOT_TOOLS/autoninja" ]]; then
    echo "SANDIUM_DEPOT_TOOLS=NOT_READY; restore/install the existing toolchain cache first." >&2
    exit 31
  fi
  export PATH="$DEPOT_TOOLS:$CHROMIUM_SRC/buildtools/linux64:$PATH"
}

check_patched_source() {
  test -d "$BARE_DIR/.git"
  test "$(git -C "$BARE_DIR" rev-parse HEAD)" = "$EXPECTED_BARE"
  test -d "$CHROMIUM_SRC/.git"
  test -s "$CHROMIUM_SRC/chrome/VERSION"
  test -s "$CHROMIUM_SRC/chrome/browser/resources/bare/ublock_origin.crx"
  local count base head version
  count="$(find "$BARE_DIR/patches" -maxdepth 1 -type f -name '*.patch' | wc -l | tr -d ' ')"
  test "$count" -gt 0
  head="$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"
  base="$(git -C "$CHROMIUM_SRC" rev-parse "HEAD~$count")"
  test "$base" = "$CHROMIUM_COMMIT" || {
    echo "SANDIUM_PATCHED_SOURCE_BASE_MISMATCH expected=$CHROMIUM_COMMIT actual=$base" >&2
    exit 34
  }
  version="$(awk -F= '/^(MAJOR|MINOR|BUILD|PATCH)=/{v[$1]=$2} END{print v["MAJOR"]"."v["MINOR"]"."v["BUILD"]"."v["PATCH"]}' "$CHROMIUM_SRC/chrome/VERSION")"
  test "$version" = "$CHROMIUM_VERSION" || {
    echo "SANDIUM_CHROMIUM_VERSION_MISMATCH expected=$CHROMIUM_VERSION actual=$version" >&2
    exit 35
  }
  echo "SANDIUM_PATCHED_CHROMIUM_HEAD=$head"
  echo "SANDIUM_PATCHED_CHROMIUM_BASE=$base"
  echo "SANDIUM_PATCH_COUNT=$count"
}

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
  source|--source)
    check_toolchain
    bash "$BARE_ADAPTER" inspect
    python3 "$CACHE_IDENTITY_TOOL"

    if [[ -d "$CHROMIUM_SRC/.git" ]]; then
      check_patched_source
      echo "SANDIUM_SOURCE_CACHE=HIT_OR_PREPARED"
      echo "CHROMIUM_SYNC_RUN=no"
      exit 0
    fi

    if [[ -e "$ROOT/.gclient" || -e "$CHROMIUM_SRC" ]]; then
      echo "SANDIUM_CHROMIUM_WORKSPACE_CONFLICT=true; refusing to overwrite existing source state." >&2
      exit 32
    fi
    if [[ "${SANDIUM_ALLOW_CHROMIUM_SYNC:-false}" != "true" ]]; then
      echo "SANDIUM_BOOTSTRAP_BLOCKED=Set SANDIUM_ALLOW_CHROMIUM_SYNC=true only after separate approval." >&2
      exit 30
    fi
    : "${BUILDBUDDY_API_KEY:?Existing protected BuildBuddy credential is unavailable; stop without creating a replacement}"

    cd "$ROOT"
    # Do not run "fetch android" here: it performs an initial dependency sync
    # before this workflow can enforce the pinned Chromium revision.
    "$DEPOT_TOOLS/gclient" config --spec "solutions = [{'name': 'src', 'url': 'https://chromium.googlesource.com/chromium/src.git'}]"
    test -f "$ROOT/.gclient"
    python3 "$SISO_ADAPTER" --workspace "$ROOT" --write

    mkdir "$CHROMIUM_SRC"
    git -C "$CHROMIUM_SRC" init
    git -C "$CHROMIUM_SRC" remote add origin https://chromium.googlesource.com/chromium/src.git
    echo "SANDIUM_CHROMIUM_DIRECT_PIN_FETCH=START"
    if ! timeout 900 git -C "$CHROMIUM_SRC" fetch --no-tags origin "$CHROMIUM_COMMIT" 2>&1 | tee "$ROOT/sandium-chromium-fetch.log"; then
      echo "SANDIUM_CHROMIUM_DIRECT_PIN_FETCH=FAIL" >&2
      exit 36
    fi
    git -C "$CHROMIUM_SRC" checkout --detach FETCH_HEAD
    actual="$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"
    if [[ "$actual" != "$CHROMIUM_COMMIT" ]]; then
      echo "SANDIUM_CHROMIUM_PIN_MISMATCH expected=$CHROMIUM_COMMIT actual=$actual" >&2
      exit 33
    fi

    echo "SANDIUM_CHROMIUM_DEPENDENCY_SYNC=START"
    if ! timeout 1200 "$DEPOT_TOOLS/gclient" sync --nohooks --revision "src@$CHROMIUM_COMMIT" 2>&1 | tee "$ROOT/sandium-gclient-sync.log"; then
      echo "SANDIUM_CHROMIUM_DEPENDENCY_SYNC=FAIL" >&2
      exit 37
    fi
    actual="$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"
    if [[ "$actual" != "$CHROMIUM_COMMIT" ]]; then
      echo "SANDIUM_CHROMIUM_PIN_MISMATCH_AFTER_SYNC expected=$CHROMIUM_COMMIT actual=$actual" >&2
      exit 38
    fi
    bash "$BARE_ADAPTER" apply
    "$DEPOT_TOOLS/gclient" runhooks
    check_patched_source
    python3 "$CACHE_IDENTITY_TOOL"
    echo "SANDIUM_CHROMIUM_SOURCE_PREPARATION=PASS"
    echo "CHROMIUM_SYNC_RUN=yes"
    echo "GN_GENERATION_RUN=no"
    echo "COMPILATION_RUN=no"
    exit 0
    ;;
  configure|--configure)
    check_toolchain
    check_patched_source
    : "${BUILDBUDDY_API_KEY:?Existing protected BuildBuddy credential is unavailable; stop without creating a replacement}"
    python3 "$SISO_ADAPTER" --workspace "$ROOT" --write
    test -x "$CHROMIUM_SRC/build/install-build-deps.sh"
    sudo "$CHROMIUM_SRC/build/install-build-deps.sh" --android

    GN="$CHROMIUM_SRC/buildtools/linux64/gn"
    test -x "$GN"
    GN_ARGS_FILE="$CONTROL_ROOT/ci/bare/gn_args.gn"
    test -s "$GN_ARGS_FILE"
    "$GN" gen "$OUT" --args="$(cat "$GN_ARGS_FILE")"

    python3 "$CACHE_IDENTITY_TOOL"
    python3 "$IDENTITY_TOOL" --mode record --root "$ROOT" --output "$ROOT/sandium-output"
    echo "SANDIUM_GN_GENERATION=PASS"
    echo "COMPILATION_RUN=no"
    echo "APK_BUILD_RUN=no"
    exit 0
    ;;
  bootstrap|--bootstrap)
    "$0" source
    "$0" configure
    exit 0
    ;;
  *)
    echo "Usage: $0 [inspect|source|configure|bootstrap]" >&2
    exit 2
    ;;
esac
