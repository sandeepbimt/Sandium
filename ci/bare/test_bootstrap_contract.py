#!/usr/bin/env python3
"""No-network regression tests for the pinned Bare Chromium bootstrap contract."""
from __future__ import annotations

import unittest
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[2]
SCRIPT = (CONTROL / "ci" / "bare" / "bootstrap_chromium.sh").read_text(encoding="utf-8")
CONFIG = (CONTROL / ".circleci" / "config.yml").read_text(encoding="utf-8")


class BootstrapContractTests(unittest.TestCase):
    def test_bootstrap_does_not_run_fetch_android_before_pin(self):
        self.assertNotIn('"$DEPOT_TOOLS/fetch" --nohooks android', SCRIPT)
        self.assertNotIn("fetch --nohooks android", SCRIPT)

    def test_gclient_config_and_siso_settings_precede_direct_pinned_fetch(self):
        config = SCRIPT.index('"$DEPOT_TOOLS/gclient" config --spec')
        siso = SCRIPT.index('python3 "$SISO_ADAPTER" --workspace "$ROOT" --write', config)
        fetch = SCRIPT.index('git -C "$CHROMIUM_SRC" fetch --no-tags origin "$CHROMIUM_COMMIT"', siso)
        checkout = SCRIPT.index('git -C "$CHROMIUM_SRC" checkout --detach FETCH_HEAD', fetch)
        sync = SCRIPT.index('"$DEPOT_TOOLS/gclient" sync --nohooks --revision "src@$CHROMIUM_COMMIT"', checkout)
        self.assertLess(config, siso)
        self.assertLess(siso, fetch)
        self.assertLess(fetch, checkout)
        self.assertLess(checkout, sync)

    def test_source_fetch_and_dependency_sync_are_bounded_and_logged(self):
        self.assertIn("timeout 900 git -C", SCRIPT)
        self.assertIn('tee "$ROOT/sandium-chromium-fetch.log"', SCRIPT)
        self.assertIn('timeout 1200 "$DEPOT_TOOLS/gclient" sync', SCRIPT)
        self.assertIn('tee "$ROOT/sandium-gclient-sync.log"', SCRIPT)

    def test_pin_is_checked_before_and_after_dependency_sync(self):
        self.assertGreaterEqual(SCRIPT.count('actual="$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"'), 2)
        self.assertIn('git -C "$CHROMIUM_SRC" checkout --detach FETCH_HEAD', SCRIPT)
        self.assertIn('if [[ "$actual" != "$CHROMIUM_COMMIT" ]]', SCRIPT)

    def test_full_build_gate_remains_closed_by_default(self):
        self.assertIn("run_sandium_bare_source_ready:\n    type: boolean\n    default: false", CONFIG)
        self.assertIn("run_sandium_bare_android_apk_build:\n    type: boolean\n    default: false", CONFIG)
        self.assertIn("SANDIUM_BARE_FULL_BUILD=BLOCKED", CONFIG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
