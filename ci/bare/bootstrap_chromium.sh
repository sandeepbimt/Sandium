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
    # fetch android itself runs gclient sync at the current tip before returning.
    # That meant the prior direct-pin step happened too late. Configure only,
    # pin Chromium directly, then do one dependency sync against the pinned DEPS.
    : > "$FETCH_LOG"
    : > "$SYNC_LOG"
    : > "$CONFIG_LOG"
    : > "$GIT_TRACE_LOG"
    export GIT_TRACE="$GIT_TRACE_LOG"
    export GIT_TERMINAL_PROMPT=0
    export GIT_CONFIG_COUNT=2
    export GIT_CONFIG_KEY_0=http.lowSpeedLimit
    export GIT_CONFIG_VALUE_0=0
    export GIT_CONFIG_KEY_1=http.lowSpeedTime
    export GIT_CONFIG_VALUE_1=300

    GCLIENT_SPEC=$(cat <<'SANDIUM_GCLIENT_SPEC'
solutions = [
  {
    "name": "src",
    "url": "https://chromium.googlesource.com/chromium/src.git",
    "custom_deps": {},
    "custom_vars": {},
  },
]
target_os = ["android"]
SANDIUM_GCLIENT_SPEC
)
    echo "SANDIUM_GCLIENT_CONFIG_BEGIN_UTC=$(date -u +%FT%TZ)"
    set +e
    "$DEPOT_TOOLS/gclient" config --spec "$GCLIENT_SPEC" 2>&1 | tee "$CONFIG_LOG"
    config_rc=${PIPESTATUS[0]}
    set -e
    echo "SANDIUM_GCLIENT_CONFIG_EXIT_CODE=$config_rc"
    echo "SANDIUM_GCLIENT_CONFIG_END_UTC=$(date -u +%FT%TZ)"
    if [[ "$config_rc" -ne 0 ]]; then
      tail -n 200 "$CONFIG_LOG" 2>/dev/null || true
      exit "$config_rc"
    fi

    echo "SANDIUM_CHROMIUM_DIRECT_PIN_BEGIN_UTC=$(date -u +%FT%TZ)"
    echo "SANDIUM_CHROMIUM_DIRECT_PIN=$CHROMIUM_COMMIT"
    mkdir -p "$CHROMIUM_SRC"
    git -C "$CHROMIUM_SRC" init
    git -C "$CHROMIUM_SRC" remote add origin https://chromium.googlesource.com/chromium/src.git
    set +e
    git -C "$CHROMIUM_SRC" -c protocol.version=2 fetch --no-tags --depth=1 origin "$CHROMIUM_COMMIT" 2>&1 | tee "$FETCH_LOG"
    fetch_rc=${PIPESTATUS[0]}
    set -e
    echo "SANDIUM_CHROMIUM_DIRECT_PIN_FETCH_EXIT_CODE=$fetch_rc"
    if [[ "$fetch_rc" -ne 0 ]]; then
      echo "SANDIUM_CHROMIUM_DIRECT_PIN_FAILURE_DIAGNOSTICS_BEGIN"
      ps -eo pid,ppid,etime,stat,%cpu,%mem,args --sort=-%cpu | head -n 60 || true
      tail -n 250 "$FETCH_LOG" 2>/dev/null || true
      tail -n 200 "$GIT_TRACE_LOG" 2>/dev/null || true
      echo "SANDIUM_CHROMIUM_DIRECT_PIN_FAILURE_DIAGNOSTICS_END"
      exit "$fetch_rc"
    fi
    git -C "$CHROMIUM_SRC" checkout --force --detach FETCH_HEAD
    actual="$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"
    if [[ "$actual" != "$CHROMIUM_COMMIT" ]]; then
      echo "SANDIUM_CHROMIUM_PIN_MISMATCH expected=$CHROMIUM_COMMIT actual=$actual" >&2
      exit 33
    fi
    echo "SANDIUM_CHROMIUM_DIRECT_PIN_PASS"
    echo "SANDIUM_CHROMIUM_DIRECT_PIN_END_UTC=$(date -u +%FT%TZ)"

    test -f "$ROOT/.gclient"
    python3 "$SISO_ADAPTER" --workspace "$ROOT" --write
    echo "SANDIUM_GCLIENT_SYNC_BEGIN_UTC=$(date -u +%FT%TZ)"
    echo "SANDIUM_GCLIENT_SYNC_COMMAND=gclient sync --verbose --no-history --nohooks --revision src@$CHROMIUM_COMMIT"
    SYNC_TIMEOUT_SECONDS="${SANDIUM_GCLIENT_SYNC_TIMEOUT_SECONDS:-1200}"
    echo "SANDIUM_GCLIENT_SYNC_TIMEOUT_SECONDS=$SYNC_TIMEOUT_SECONDS"
    set +e
    timeout --signal=TERM --kill-after=60s "${SYNC_TIMEOUT_SECONDS}s" "$DEPOT_TOOLS/gclient" sync --verbose --no-history --nohooks --revision "src@$CHROMIUM_COMMIT" 2>&1 | tee "$SYNC_LOG"
    sync_rc=${PIPESTATUS[0]}
    set -e
    echo "SANDIUM_GCLIENT_SYNC_END_UTC=$(date -u +%FT%TZ)"
    echo "SANDIUM_GCLIENT_SYNC_EXIT_CODE=$sync_rc"
    if [[ "$sync_rc" -ne 0 ]]; then
      echo "SANDIUM_GCLIENT_FAILURE_DIAGNOSTICS_BEGIN"
      echo "SANDIUM_GCLIENT_FAILURE_KIND=$( [[ "$sync_rc" -eq 124 || "$sync_rc" -eq 137 ]] && echo TIMEOUT || echo COMMAND_FAILURE )"
      echo "SANDIUM_GCLIENT_FAILURE_EXIT_CODE=$sync_rc"
      echo "--- processes ---"
      ps -eo pid,ppid,etime,stat,%cpu,%mem,wchan:24,args --sort=-%cpu | head -n 80 || true
      echo "--- network sockets ---"
      if command -v ss >/dev/null 2>&1; then ss -tpn state established 2>&1 | head -n 100 || true; else echo "ss unavailable"; fi
      echo "--- git status ---"
      git -C "$CHROMIUM_SRC" status --short --branch 2>&1 || true
      echo "--- lock files ---"
      find "$ROOT" -maxdepth 4 \( -name "*.lock" -o -name "*.lck" \) -print 2>/dev/null | head -n 150 || true
      echo "--- sync log tail ---"
      tail -n 400 "$SYNC_LOG" 2>/dev/null || true
      echo "--- git trace tail ---"
      tail -n 400 "$GIT_TRACE_LOG" 2>/dev/null || true
      echo "SANDIUM_GCLIENT_FAILURE_DIAGNOSTICS_END"
      exit "$sync_rc"
    fi

    actual="$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"
    if [[ "$actual" != "$CHROMIUM_COMMIT" ]]; then
      echo "SANDIUM_CHROMIUM_PIN_MISMATCH expected=$CHROMIUM_COMMIT actual=$actual" >&2
      exit 33
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
