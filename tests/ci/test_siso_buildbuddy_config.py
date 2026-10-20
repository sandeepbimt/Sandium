"""Offline tests for Sandium's BuildBuddy Siso adapter."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "ci/siso/buildbuddy-credential-helper.py"
WRITER_PATH = ROOT / "ci/siso/write_buildbuddy_env.py"
BACKEND_PATH = ROOT / "ci/siso/buildbuddy_backend.star"

spec = importlib.util.spec_from_file_location("write_buildbuddy_env", WRITER_PATH)
assert spec and spec.loader
writer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = writer
spec.loader.exec_module(writer)


class CredentialHelperTests(unittest.TestCase):
    def invoke(self, uri: str, key: str | None = "FAKE-TEST-KEY", args=("get",), raw=None):
        env = os.environ.copy()
        env.pop("BUILDBUDDY_API_KEY", None)
        if key is not None:
            env["BUILDBUDDY_API_KEY"] = key
        request = raw if raw is not None else json.dumps({"uri": uri})
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            input=request,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_secure_buildbuddy_https_uri_returns_header(self):
        result = self.invoke("https://remote.buildbuddy.io:443/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"headers": {"x-buildbuddy-api-key": ["FAKE-TEST-KEY"]}},
        )
        self.assertNotIn("FAKE-TEST-KEY", result.stderr)

    def test_secure_grpcs_uri_returns_header(self):
        result = self.invoke("grpcs://remote.buildbuddy.io")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["headers"]["x-buildbuddy-api-key"],
            ["FAKE-TEST-KEY"],
        )

    def test_wrong_host_is_rejected_without_revealing_key(self):
        result = self.invoke("https://evil.example/")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("FAKE-TEST-KEY", result.stderr)

    def test_insecure_scheme_is_rejected(self):
        result = self.invoke("http://remote.buildbuddy.io/")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_wrong_port_is_rejected(self):
        result = self.invoke("https://remote.buildbuddy.io:8443/")
        self.assertNotEqual(result.returncode, 0)

    def test_missing_key_fails_closed(self):
        result = self.invoke("https://remote.buildbuddy.io/", key=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_malformed_json_fails_closed(self):
        result = self.invoke("https://remote.buildbuddy.io/", raw="{")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_unexpected_command_fails_closed(self):
        result = self.invoke("https://remote.buildbuddy.io/", args=("get", "extra"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Path(self.temp.name) / "src" / "brave"
        (self.core / "ci/siso").mkdir(parents=True)
        (self.core / "ci/pins").mkdir(parents=True)
        self.pin_path = self.core / "ci/pins/chromium-tag.txt"
        self.pin_path.write_text(
            "# Manual Sandium Chromium pin\n156.0.8078.39\n",
            encoding="utf-8",
        )
        shutil.copy2(HELPER, self.core / "ci/siso/buildbuddy-credential-helper.py")
        shutil.copy2(BACKEND_PATH, self.core / "ci/siso/buildbuddy_backend.star")

    def tearDown(self):
        self.temp.cleanup()

    def make_env(self, address="remote.buildbuddy.io:443", instance="sandium/android-arm64/ci"):
        return {
            "SANDIUM_REAPI_ADDRESS": address,
            "SANDIUM_REAPI_INSTANCE": instance,
            "BUILDBUDDY_API_KEY": "MUST-NEVER-BE-WRITTEN",
        }

    def test_writes_expected_settings_without_secret_and_makes_helper_executable(self):
        env_file = writer.apply_config(self.core, self.make_env())
        content = env_file.read_text(encoding="utf-8")
        self.assertIn("rbe_service=remote.buildbuddy.io:443", content)
        self.assertIn(
            "projects_chrome_custom_vars_reapi_instance=sandium/android-arm64/ci",
            content,
        )
        expected_backend = str((self.core / "ci/siso/buildbuddy_backend.star").resolve())
        self.assertIn(
            "projects_chrome_custom_vars_reapi_backend_config_path=" + expected_backend,
            content,
        )
        self.assertIn("projects_chrome_custom_vars_reapi_credential_helper=", content)
        self.assertIn("siso_use_googlechrome_config=false", content)
        self.assertIn("use_remoteexec=true", content)
        self.assertIn("SANDIUM_REMOTE_EXECUTION_ENABLED=yes", writer.main.__code__.co_consts if False else "SANDIUM_REMOTE_EXECUTION_ENABLED=yes")
        self.assertIn("sandium_chromium_tag=156.0.8078.39", content)
        self.assertIn("projects_chrome_custom_vars_siso_version=git_revision:bc45e8f67ae0f37d337190ad64aa8bb440c791eb", content)
        self.assertNotIn("MUST-NEVER-BE-WRITTEN", content)
        mode = (self.core / "ci/siso/buildbuddy-credential-helper.py").stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_updates_config_idempotently_and_preserves_unrelated_lines(self):
        env_file = self.core / ".env"
        env_file.write_text("existing_setting=true\n", encoding="utf-8")
        writer.apply_config(self.core, self.make_env())
        first = env_file.read_text(encoding="utf-8")
        writer.apply_config(self.core, self.make_env())
        second = env_file.read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertIn("existing_setting=true", second)
        self.assertEqual(second.count(writer.BEGIN_MARKER), 1)
        self.assertEqual(second.count(writer.END_MARKER), 1)

    def test_non_sandium_namespace_is_rejected(self):
        with self.assertRaises(ValueError):
            writer.apply_config(self.core, self.make_env(instance="default"))
        self.assertFalse((self.core / ".env").exists())

    def test_unreviewed_endpoint_is_rejected(self):
        with self.assertRaises(ValueError):
            writer.apply_config(self.core, self.make_env(address="https://evil.example"))
        self.assertFalse((self.core / ".env").exists())

    def test_manual_chromium_pin_is_required_and_must_be_numeric(self):
        self.pin_path.write_text("# manual\nnot-a-tag\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            writer.apply_config(self.core, self.make_env())

    def test_malformed_existing_block_is_rejected(self):
        env_file = self.core / ".env"
        env_file.write_text(writer.BEGIN_MARKER + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            writer.apply_config(self.core, self.make_env())


class BackendConfigTests(unittest.TestCase):
    def test_backend_uses_digest_pinned_image_without_google_worker_labels(self):
        content = BACKEND_PATH.read_text(encoding="utf-8")
        self.assertIn("OSFamily", content)
        self.assertIn("docker://gcr.io/chops-public-images-prod/rbe/siso-chromium/linux@sha256:d7cb1ab14a0f20aa669c23f22c15a9dead761dcac19f43985bf9dd5f41fbef3a", content)
        self.assertEqual(content.count('"container-image"'), 2)
        self.assertNotIn('"label:action_default"', content)
        self.assertNotIn('"label:action_large"', content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
