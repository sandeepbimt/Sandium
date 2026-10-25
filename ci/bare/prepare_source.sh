#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

# Bare is the sole active browser-source input. This script never checks out or
# builds Chromium; Chromium must already exist at the exact pinned commit for --apply.
BARE_REPO_URL="https://github.com/BareBrowser/bare-browser.git"
BARE_COMMIT="a5a7ef74f3b77d45720b8c502cbacc30f2f3dbd5"
CHROMIUM_COMMIT="1228231501697ff7a4a0150ea8ae6df37bb99331"
CHROMIUM_VERSION="156.0.8078.25"
EXPECTED_PATCH_COUNT=164

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
# The existing CircleCI workflow checks the Sandium control repository out under
# $HOME/project/brave-core and keeps Chromium at $HOME/project/src. Infer that
# existing workspace layout without adding new CircleCI variables.
if [[ "$(basename "$CONTROL_ROOT")" == "brave-core" ]]; then
  ROOT="$(cd "$CONTROL_ROOT/.." && pwd)"
else
  ROOT="$CONTROL_ROOT"
fi
BARE_DIR="${SANDIUM_BARE_DIR:-$ROOT/bare-browser}"
MODE="${1:-inspect}"

case "$MODE" in
  inspect|--inspect)
    ;;
  apply|--apply)
    ;;
  *)
    echo "Usage: $0 [inspect|apply]" >&2
    exit 2
    ;;
esac

if [[ ! -e "$BARE_DIR" ]]; then
  mkdir -p "$BARE_DIR"
  git -C "$BARE_DIR" init
  git -C "$BARE_DIR" remote add origin "$BARE_REPO_URL"
  git -C "$BARE_DIR" fetch --depth=1 origin "$BARE_COMMIT"
  git -C "$BARE_DIR" checkout --detach FETCH_HEAD
elif [[ ! -d "$BARE_DIR/.git" ]]; then
  echo "BARE_DIR_EXISTS_BUT_IS_NOT_A_GIT_CHECKOUT path=$BARE_DIR" >&2
  exit 9
fi

actual_bare_commit="$(git -C "$BARE_DIR" rev-parse HEAD)"
if [[ "$actual_bare_commit" != "$BARE_COMMIT" ]]; then
  echo "BARE_COMMIT_MISMATCH expected=$BARE_COMMIT actual=$actual_bare_commit" >&2
  exit 10
fi

BUILDING="$BARE_DIR/BUILDING.md"
README="$BARE_DIR/README.md"
PATCH_DIR="$BARE_DIR/patches"
CRX="$BARE_DIR/third_party/ublock_origin/uBlockOrigin-1.75.0.crx"
CRX_CHECKSUM="$BARE_DIR/third_party/ublock_origin/uBlockOrigin-1.75.0.crx.sha256"

test -s "$BUILDING"
test -s "$README"
test -d "$PATCH_DIR"
grep -Fq "$CHROMIUM_COMMIT" "$BUILDING"
grep -Fq "$CHROMIUM_VERSION" "$BUILDING"
grep -Fq 'git am' "$README"

patch_count="$(find "$PATCH_DIR" -maxdepth 1 -type f -name '*.patch' | wc -l | tr -d ' ')"
if [[ "$patch_count" != "$EXPECTED_PATCH_COUNT" ]]; then
  echo "BARE_PATCH_COUNT_MISMATCH expected=$EXPECTED_PATCH_COUNT actual=$patch_count" >&2
  exit 11
fi
patch_digest="$(find "$PATCH_DIR" -maxdepth 1 -type f -name '*.patch' -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')"

test -s "$CRX"
test -s "$CRX_CHECKSUM"
(
  cd "$BARE_DIR"
  sha256sum -c third_party/ublock_origin/uBlockOrigin-1.75.0.crx.sha256
)

echo "BARE_COMMIT=$actual_bare_commit"
echo "CHROMIUM_COMMIT=$CHROMIUM_COMMIT"
echo "CHROMIUM_VERSION=$CHROMIUM_VERSION"
echo "BARE_PATCH_COUNT=$patch_count"
echo "BARE_PATCH_SERIES_SHA256=$patch_digest"
echo "UBLOCK_VERSION=1.75.0"
echo "UBLOCK_CRX_SHA256=$(sha256sum "$CRX" | awk '{print $1}')"

if [[ "$MODE" == "inspect" || "$MODE" == "--inspect" ]]; then
  echo "BARE_SOURCE_INSPECTION=PASS"
  echo "CHROMIUM_CHECKOUT_OR_BUILD=NOT_RUN"
  exit 0
fi

CHROMIUM_SRC="${CHROMIUM_SRC:-$ROOT/src}"
if [[ ! -d "$CHROMIUM_SRC/.git" ]]; then
  echo "CHROMIUM_CHECKOUT_MISSING path=$CHROMIUM_SRC; this script does not download Chromium." >&2
  exit 20
fi
actual_chromium_commit="$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"
if [[ "$actual_chromium_commit" != "$CHROMIUM_COMMIT" ]]; then
  echo "CHROMIUM_COMMIT_MISMATCH expected=$CHROMIUM_COMMIT actual=$actual_chromium_commit" >&2
  exit 21
fi
if [[ -n "$(git -C "$CHROMIUM_SRC" status --porcelain)" ]]; then
  echo "CHROMIUM_WORKTREE_NOT_CLEAN; refusing to apply the patch series." >&2
  exit 22
fi

# Patch filenames are zero-padded and sorted under LC_ALL=C for deterministic order.
if ! git -C "$CHROMIUM_SRC" am --3way "$PATCH_DIR"/*.patch; then
  git -C "$CHROMIUM_SRC" am --abort || true
  echo "BARE_PATCH_APPLICATION=FAIL; patch application was aborted to restore the original checkout." >&2
  exit 23
fi

target_dir="$CHROMIUM_SRC/chrome/browser/resources/bare"
mkdir -p "$target_dir"
install -m 0644 "$CRX" "$target_dir/ublock_origin.crx"

echo "BARE_PATCH_APPLICATION=PASS"
echo "PATCHED_CHROMIUM_HEAD=$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"
echo "UBLOCK_CRX_INSTALL=PASS"
echo "CHROMIUM_BUILD=NOT_RUN"
