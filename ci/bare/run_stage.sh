#!/usr/bin/env bash
# Schema-2 five-stage runner. This is deliberately not wired to CircleCI yet:
# a reviewed persistence layer must restore the same pinned source/toolchain and
# stage ledger before stages 2-5 are enabled.
set -Eeuo pipefail
set +x

STAGE="${1:-}"
[[ "$STAGE" =~ ^[1-5]$ ]] || { echo "Usage: $0 <1..5>" >&2; exit 2; }
[[ "${SANDIUM_BARE_STAGE_RUN_APPROVED:-false}" == "true" ]] || {
  echo "SANDIUM_BARE_STAGE_RUN=BLOCKED"
  echo "Reason: stage runner requires explicit approval and is not enabled by the default CI workflow."
  exit 78
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
if [[ "$CONTROL" == "$HOME/project/"* ]]; then ROOT="$HOME/project"; else ROOT="$CONTROL"; fi
PREV_STAGE=$((STAGE - 1))
HANDOFF="$ROOT/ci-handoff/stage-$STAGE"
LEDGER="$HANDOFF/ledger.json"
PREV="$ROOT/ci-handoff/stage-$PREV_STAGE/ledger.json"
SOURCE_LOG="$ROOT/sandium-source-preparation.log"
METRICS="$ROOT/stage-$STAGE-metrics.jsonl"
SISO_LOG="$ROOT/stage-$STAGE-siso.log"
OUT="$ROOT/src/out/android_Debug_arm64"
mkdir -p "$HANDOFF"
DRIVER="$ROOT/stage-$STAGE-driver.log"
exec > >(tee -a "$DRIVER") 2>&1

fail() { echo "SANDIUM_STAGE_RUN=FAIL reason=$*" >&2; exit 1; }
need_file() { [[ -s "$1" ]] || fail "required file missing or empty: $1"; }
need_checkout() {
  [[ -d "$ROOT/src/.git" ]] || fail "pinned Chromium checkout is not present in the restored stage workspace"
  [[ -d "$ROOT/bare-browser/.git" ]] || fail "pinned Bare checkout is not present in the restored stage workspace"
  [[ -s "$ROOT/.gclient" ]] || fail "generated .gclient is not present in the restored stage workspace"
}

ensure_depot_tools() {
  local depot="$HOME/depot_tools" pin=""
  if [[ "$STAGE" -gt 1 ]]; then
    need_file "$PREV"
    pin="$(python3 - "$PREV" <<'PY'
import json, sys
from pathlib import Path
ledger = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(ledger.get("build_identity", {}).get("toolchain_depot_tools_commit", ""))
PY
)"
    [[ "$pin" =~ ^[0-9a-f]{40}$ ]] || fail "previous ledger has no valid depot_tools commit pin"
  fi
  if [[ ! -d "$depot/.git" ]]; then
    [[ "$STAGE" == "1" ]] || fail "depot_tools checkout missing; stage workspace persistence is incomplete"
    git clone --no-tags https://chromium.googlesource.com/chromium/tools/depot_tools.git "$depot"
  fi
  if [[ "$STAGE" -gt 1 && "$(git -C "$depot" rev-parse HEAD)" != "$pin" ]]; then
    git -C "$depot" fetch --no-tags origin "$pin"
    git -C "$depot" checkout --force --detach "$pin"
  fi
  export PATH="$depot:$ROOT/src/buildtools/linux64:$PATH"
  export DEPOT_TOOLS_UPDATE=0
  if [[ ! -s "$depot/python3_bin_reldir.txt" ]]; then
    if [[ -x "$depot/update_depot_tools" ]]; then (cd "$depot" && ./update_depot_tools)
    elif [[ -x "$depot/ensure_bootstrap" ]]; then (cd "$depot" && ./ensure_bootstrap)
    else fail "depot_tools bootstrap helper is unavailable"
    fi
  fi
  for tool in fetch gclient autoninja; do [[ -x "$depot/$tool" ]] || fail "depot_tools tool missing: $tool"; done
}

validate_handoff() {
  # Validate the previous schema-2 ledger against the current pinned identity
  # before any stage-local compile work is allowed to spend time or credits.
  python3 - "$CONTROL/ci/bare/stage_ledger.py" "$PREV" "$STAGE" "$ROOT" <<'PY'
import importlib.util, sys
from pathlib import Path
ledger_module_path, prev_path, stage_text, root_text = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("sandium_stage_ledger_handoff", ledger_module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
stage, root = int(stage_text), Path(root_text).resolve()
control = Path(ledger_module_path).resolve().parents[2]
identity = module.source_identity(control, root, require_gn=(stage >= 2))
prior = module.load_ledger(Path(prev_path))
module.validate_previous(
    stage, prior, module.digest(module.stable_identity(identity)),
    identity.get("gn_args_sha256")
)
print(f"SANDIUM_STAGE_HANDOFF=PASS previous_stage={stage - 1} current_stage={stage} schema={module.SCHEMA}")
PY
}

echo "SANDIUM_STAGE=$STAGE"
echo "SANDIUM_STAGE_STARTED_UTC=$(date -u +%FT%TZ)"
echo "SANDIUM_CONTROL_HEAD=$(git -C "$CONTROL" rev-parse HEAD)"
echo "SANDIUM_SHARED_WORKSPACE=$ROOT"

ensure_depot_tools

case "$STAGE" in
  1)
    # Stage 1 owns source/dependency readiness only. No GN generation or Siso.
    rm -f "$LEDGER"
    : > "$SOURCE_LOG"
    if ! bash "$CONTROL/ci/bare/bootstrap_chromium.sh" source 2>&1 | tee "$SOURCE_LOG"; then
      fail "pinned Bare/Chromium source preparation failed"
    fi
    grep -Eq 'SANDIUM_CHROMIUM_SOURCE_PREPARATION=PASS|SANDIUM_SOURCE_CACHE=HIT_OR_PREPARED' "$SOURCE_LOG" ||
      fail "source preparation returned no positive readiness marker"
    python3 "$CONTROL/ci/bare/stage_ledger.py" \
      --stage 1 --source-log "$SOURCE_LOG" --output "$LEDGER" --root "$ROOT"
    echo "CHROMIUM_SYNC_OR_CACHE_PREPARATION=PASS"
    echo "GN_GENERATION_RUN=no"
    echo "SISO_BUILD_RUN=no"
    ;;
  2)
    need_file "$PREV"
    need_checkout
    # Stage 2 owns generated GN/build graph readiness only.
    bash "$CONTROL/ci/bare/bootstrap_chromium.sh" configure
    need_file "$OUT/args.gn"
    need_file "$OUT/build.ninja"
    validate_handoff
    python3 "$CONTROL/ci/bare/stage_ledger.py" \
      --stage 2 --previous "$PREV" --output "$LEDGER" --root "$ROOT"
    echo "GN_GENERATION=PASS"
    echo "SISO_BUILD_RUN=no"
    ;;
  3|4)
    need_file "$PREV"
    need_checkout
    need_file "$OUT/args.gn"
    need_file "$OUT/build.ninja"
    validate_handoff
    export DEPOT_TOOLS_UPDATE=0
    export SISO_LIMITS="${SANDIUM_SISO_LIMITS:-local=8,remote=80}"
    : > "$METRICS"
    : > "$SISO_LOG"
    local_seconds="${SANDIUM_STAGE_BUILD_SECONDS:-2700}"
    [[ "$local_seconds" =~ ^[0-9]+$ ]] && (( local_seconds >= 120 )) || fail "invalid stage build timeout"
    set +e
    timeout --signal=INT --kill-after=60s "${local_seconds}s" \
      bash -o pipefail -c 'cd "$1" && autoninja -strict_remote -metrics_json "$2" -C "$3" -j"$4" chrome_public_apk 2>&1 | tee "$5"' \
      _ "$ROOT/src" "$METRICS" "$OUT" "${SANDIUM_SISO_JOBS:-80}" "$SISO_LOG"
    build_rc=$?
    set -e
    echo "SANDIUM_AUTONINJA_EXIT_CODE=$build_rc"
    # 124 means the planned time slice ended. The schema-2 ledger below still
    # requires real stage-local remote action evidence; stage 4 additionally
    # requires both cache hits and new remote actions in this invocation.
    [[ "$build_rc" == "0" || "$build_rc" == "124" ]] ||
      fail "Siso invocation exited unsuccessfully (exit=$build_rc)"
    python3 "$CONTROL/ci/bare/stage_ledger.py" \
      --stage "$STAGE" --metrics "$METRICS" --siso-log "$SISO_LOG" \
      --previous "$PREV" --output "$LEDGER" --root "$ROOT"
    echo "SANDIUM_STAGE_LOCAL_SISO_EVIDENCE=PASS"
    ;;
  5)
    need_file "$PREV"
    need_checkout
    need_file "$OUT/apks/ChromePublic.apk"
    validate_handoff
    # Packaging performs an initial verification; the schema-2 ledger directly
    # re-runs ZIP/manifest/DEX checks and apksigner verification after packaging.
    bash "$CONTROL/ci/bare/package_artifact.sh"
    python3 "$CONTROL/ci/bare/stage_ledger.py" \
      --stage 5 --previous "$PREV" \
      --artifact "$ROOT/sandium-output/Sandium-debug-arm64.apk" \
      --manifest "$ROOT/sandium-output/build-manifest.json" \
      --output "$LEDGER" --root "$ROOT"
    echo "SANDIUM_APK_STAGE_VERIFICATION=PASS"
    ;;
esac

echo "SANDIUM_STAGE_LEDGER_PATH=$LEDGER"
echo "SANDIUM_STAGE_FINISHED_UTC=$(date -u +%FT%TZ)"
echo "SANDIUM_STAGE_RUN=PASS stage=$STAGE"
