#!/usr/bin/env python3
"""Guard Chromium Siso against invalid parent-relative auxiliary output paths.

BuildBuddy REAPI rejects declared output paths containing '..' path components.
Some Chromium Clang commands use -fcrash-diagnostics-dir=../clang-crashreports.
Siso's canonpath can preserve that path when it points outside the action exec root.
Crash-report upload is diagnostic-only, so the safe response is to skip registering
that auxiliary output rather than failing the actual compile action.

This is an idempotent compatibility patch against the pinned Chromium file. It
fails closed if upstream changes the expected code shape, preventing a source update
from silently removing the guard and wasting an expensive build window.
"""
from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path

TARGET_RELATIVE = Path("build/config/siso/clang_all.star")
ANCHOR = (
    "        crash_dir = ctx.fs.canonpath(crash_dir)\n"
    "        ctx.actions.fix(auxiliary_log_output_dirs = [crash_dir])"
)
MARKER = "SANDIUM_REAPI_OUTPUT_PATH_GUARD"
GUARDED_BLOCK = (
    "        crash_dir = ctx.fs.canonpath(crash_dir)\n"
    "        # " + MARKER + ": REAPI output paths must not contain parent components.\n"
    "        # Clang may request ../clang-crashreports, outside the action exec root.\n"
    "        # Crash-report capture is best-effort; never let it invalidate compilation.\n"
    "        if \"..\" in crash_dir.split(\"/\"):\n"
    "            return\n"
    "        if \"..\" in crash_dir.split(\"\\\\\"):\n"
    "            return\n"
    "        ctx.actions.fix(auxiliary_log_output_dirs = [crash_dir])"
)


class GuardError(RuntimeError):
    pass


def has_parent_component(path_value: str) -> bool:
    """Return True for a whole '..' path component using either separator."""
    return any(".." in path_value.split(separator) for separator in ("/", "\\"))


def patch_text(text: str) -> tuple[str, bool]:
    if MARKER in text:
        if text.count(MARKER) != 1 or GUARDED_BLOCK not in text:
            raise GuardError("Siso path guard marker exists but its complete guard block is missing or duplicated")
        return text, False

    occurrences = text.count(ANCHOR)
    if occurrences != 1:
        raise GuardError(
            "Pinned Chromium clang_all.star has an unexpected auxiliary-output rule "
            f"(expected one anchor, found {occurrences}); refusing to edit blindly"
        )
    return text.replace(ANCHOR, GUARDED_BLOCK, 1), True


def apply_guard(source_root: Path) -> str:
    source_root = source_root.resolve()
    target = source_root / TARGET_RELATIVE
    if not target.is_file() or target.is_symlink():
        raise GuardError(f"Missing or unsafe Siso config: {target}")
    original = target.read_text(encoding="utf-8")
    updated, changed = patch_text(original)
    if changed:
        mode = stat.S_IMODE(target.stat().st_mode)
        fd, temp_name = tempfile.mkstemp(prefix=".clang_all.star.sandium.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_name, mode)
            os.replace(temp_name, target)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    # Re-read the written file; do not report success from an in-memory edit alone.
    verified = target.read_text(encoding="utf-8")
    _, still_changed = patch_text(verified)
    if still_changed or MARKER not in verified:
        raise GuardError("Siso path guard did not persist")
    return "APPLIED" if changed else "ALREADY_APPLIED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        state = apply_guard(args.source_root)
    except (OSError, UnicodeError, GuardError) as exc:
        raise SystemExit(f"SANDIUM_SISO_OUTPUT_PATH_GUARD=FAIL {exc}") from exc
    print(f"SANDIUM_SISO_OUTPUT_PATH_GUARD={state}")
    print("SANDIUM_REAPI_PARENT_OUTPUT_PATHS=REJECTED_BEFORE_BUILD")
    print("CHROMIUM_SOURCE_REVISION_CHANGED=no")
    print("SISO_RUN=no")
    print("CHROMIUM_BUILD_RUN=no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
