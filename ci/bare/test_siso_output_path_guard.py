#!/usr/bin/env python3
"""Offline tests for the Siso auxiliary-output path guard."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import guard_siso_output_paths as guard


class GuardTests(unittest.TestCase):
    def test_detects_parent_component_with_posix_separator(self):
        self.assertTrue(guard.has_parent_component("../clang-crashreports"))
        self.assertTrue(guard.has_parent_component("sub/../clang-crashreports"))
        self.assertFalse(guard.has_parent_component("clang-crashreports"))
        self.assertFalse(guard.has_parent_component("nested/clang-crashreports"))

    def test_detects_parent_component_with_windows_separator(self):
        self.assertTrue(guard.has_parent_component(r"..\clang-crashreports"))
        self.assertTrue(guard.has_parent_component(r"sub\..\clang-crashreports"))
        self.assertFalse(guard.has_parent_component(r"clang-crashreports"))

    def test_injects_guard_after_canonicalization(self):
        fixture = (
            "def __check_crash_diagnostics(ctx, args):\n"
            + guard.ANCHOR
            + "\n"
        )
        patched, changed = guard.patch_text(fixture)
        self.assertTrue(changed)
        self.assertLess(
            patched.index("ctx.fs.canonpath(crash_dir)"),
            patched.index('if ".." in crash_dir.split("/")'),
        )
        self.assertIn(guard.MARKER, patched)
        self.assertIn(guard.GUARDED_BLOCK, patched)
        self.assertEqual(patched.count(guard.MARKER), 1)

    def test_patch_is_idempotent(self):
        fixture = "prefix\n" + guard.ANCHOR + "\nsuffix\n"
        once, changed_once = guard.patch_text(fixture)
        twice, changed_twice = guard.patch_text(once)
        self.assertTrue(changed_once)
        self.assertFalse(changed_twice)
        self.assertEqual(once, twice)

    def test_unexpected_upstream_shape_fails_closed(self):
        with self.assertRaises(guard.GuardError):
            guard.patch_text("def __check_crash_diagnostics(ctx, args):\npass\n")

    def test_incomplete_existing_guard_fails_closed(self):
        broken = (
            "prefix\n"
            + guard.MARKER
            + '\nif ".." in crash_dir.split("/"):\n'
        )
        with self.assertRaises(guard.GuardError):
            guard.patch_text(broken)

    def test_apply_is_idempotent_on_a_temporary_source_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / guard.TARGET_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_text("prefix\n" + guard.ANCHOR + "\nsuffix\n", encoding="utf-8")
            self.assertEqual(guard.apply_guard(root), "APPLIED")
            first = target.read_text(encoding="utf-8")
            self.assertEqual(guard.apply_guard(root), "ALREADY_APPLIED")
            self.assertEqual(target.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
