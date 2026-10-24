#!/usr/bin/env python3
"""Restore the highest-progress compatible checkpoint from earlier CircleCI workflows.

CircleCI cache prefix restoration is a useful fallback, but it cannot enumerate
all historical checkpoints or rank them by completed work. Published artifacts
provide that catalog when a personal CircleCI API token is available.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from checkpoint_catalog import rank

API = "https://circleci.com/api/v2"
JOB_RE = re.compile(r"stage-[1-5]-")
META_RE = re.compile(r"(?:^|/)checkpoint-artifact/sandium-checkpoint\.meta$")
ARCHIVE_RE = re.compile(r"(?:^|/)checkpoint-artifact/sandium-checkpoint\.tar\.zst$")
IDENTITY_FIELDS = (
    "source_commit", "chromium_commit", "chromium_version", "target",
    "build_config", "build_input_fingerprint", "gn_args_sha256",
)


def valid_manifest(meta: Any) -> bool:
    """Reject malformed checkpoint metadata before it can be ranked or restored."""
    if not isinstance(meta, dict) or meta.get("schema") != 1:
        return False
    if not re.fullmatch(r"stage-[1-5]", str(meta.get("stage", ""))):
        return False
    if meta.get("output_rel_path") != "out/android_Debug_arm64":
        return False
    if meta.get("target") != "android-arm64-debug-apk" or meta.get("build_config") != "Debug":
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", str(meta.get("source_commit", ""))):
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", str(meta.get("chromium_commit", ""))):
        return False
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(meta.get("chromium_version", ""))):
        return False
    for field in ("build_input_fingerprint", "gn_args_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(meta.get(field, ""))):
            return False
    for field in (*IDENTITY_FIELDS, "archive_sha256"):
        value = meta.get(field)
        if not isinstance(value, str) or not value:
            return False
    if not re.fullmatch(r"[0-9a-f]{64}", meta["archive_sha256"]):
        return False
    progress = meta.get("progress")
    if not isinstance(progress, dict):
        return False
    for field in ("compile_complete", "artifact_verified"):
        if not isinstance(progress.get(field), bool):
            return False
    for field in ("object_file_count", "linked_artifact_count", "output_bytes"):
        value = progress.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    size = meta.get("archive_bytes")
    return isinstance(size, int) and not isinstance(size, bool) and size > 0


def sha256_file(path: Path) -> str:
    """Hash large checkpoint archives incrementally rather than loading them into RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_integrity_matches(meta: Any, archive_path: Path) -> bool:
    """Validate metadata, byte count, and archive digest without a large allocation."""
    try:
        return (
            valid_manifest(meta)
            and archive_path.is_file()
            and archive_path.stat().st_size == meta["archive_bytes"]
            and sha256_file(archive_path) == meta["archive_sha256"]
        )
    except OSError:
        return False


def local_checkpoint(root: Path) -> tuple[dict[str, Any], Path, Path] | None:
    """Return a validated cache checkpoint already restored by CircleCI, if present."""
    meta_path = root / "sandium-checkpoint.meta"
    archive_path = root / "sandium-checkpoint.tar.zst"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not archive_integrity_matches(meta, archive_path):
        return None
    return meta, archive_path, meta_path


def get_json(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Circle-Token": token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CircleCI returned invalid JSON object")
    return value


def pages(url: str, token: str, key: str):
    base, _, query = url.partition("?")
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    visited: set[str] = set()
    while True:
        current = base + ("?" + urllib.parse.urlencode({k: v[-1] for k, v in params.items()}) if params else "")
        data = get_json(current, token)
        for item in data.get(key, []):
            if isinstance(item, dict):
                yield item
        next_token = data.get("next_page_token")
        if not next_token or str(next_token) in visited:
            break
        visited.add(str(next_token))
        params["page-token"] = [str(next_token)]


def fetch(url: str, path: Path) -> None:
    """Fetch a signed artifact URL without ever forwarding the API token to it."""
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("non-HTTPS artifact URL rejected")
    with urllib.request.urlopen(urllib.request.Request(url), timeout=180) as response, path.open("wb") as out:
        shutil.copyfileobj(response, out)


def discover_remote_urls(slug: str, branch: str, token: str) -> list[tuple[str, str, str]]:
    """List all discoverable metadata/archive URL pairs from recent build-stage jobs."""
    slugq = urllib.parse.quote(slug, safe="/")
    url = f"{API}/project/{slugq}/pipeline?branch={urllib.parse.quote(branch)}&page-size=100"
    candidates: list[tuple[str, str, str]] = []
    artifact_listing_errors: list[str] = []
    for index, pipeline in enumerate(pages(url, token, "items")):
        if index >= 40:
            break
        pipeline_id = pipeline.get("id")
        if not pipeline_id:
            continue
        workflow_url = f"{API}/pipeline/{urllib.parse.quote(str(pipeline_id), safe='')}/workflow"
        for workflow in pages(workflow_url, token, "items"):
            workflow_id = workflow.get("id")
            if not workflow_id:
                continue
            job_url = f"{API}/workflow/{urllib.parse.quote(str(workflow_id), safe='')}/job"
            for job in pages(job_url, token, "items"):
                job_name, job_number = str(job.get("name", "")), job.get("job_number")
                if not JOB_RE.match(job_name) or not job_number:
                    continue
                # The job's own project_slug is authoritative for its artifact
                # endpoint. Older workflows may have no artifacts at all (404),
                # which is an expected historical miss, not an API outage.
                artifact_slugs = []
                job_slug = str(job.get("project_slug", "")).strip()
                for candidate_slug in (job_slug, slug):
                    if candidate_slug and candidate_slug not in artifact_slugs:
                        artifact_slugs.append(candidate_slug)
                artifacts = None
                last_error = None
                for artifact_slug in artifact_slugs:
                    artifact_project = urllib.parse.quote(artifact_slug, safe="/")
                    artifacts_url = f"{API}/project/{artifact_project}/{job_number}/artifacts"
                    try:
                        artifacts = list(pages(artifacts_url, token, "items"))
                        break
                    except urllib.error.HTTPError as exc:
                        if exc.code == 404:
                            last_error = exc
                            continue
                        last_error = exc
                        break
                    except Exception as exc:
                        last_error = exc
                        break
                if artifacts is None:
                    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 404:
                        print(f"ARTIFACT_LIST_NO_ARTIFACTS job={job_number} status=404")
                        continue
                    reason = f"job={job_number} reason={type(last_error).__name__ if last_error else 'unknown'}"
                    artifact_listing_errors.append(reason)
                    print(f"ARTIFACT_LIST_SKIPPED {reason}")
                    continue
                meta_artifact = next((item for item in artifacts if META_RE.search(str(item.get("path", ""))) and item.get("url")), None)
                archive_artifact = next((item for item in artifacts if ARCHIVE_RE.search(str(item.get("path", ""))) and item.get("url")), None)
                if meta_artifact and archive_artifact:
                    candidates.append((str(meta_artifact["url"]), str(archive_artifact["url"]), str(job_number)))
    # Any failed listing makes the historical inventory incomplete. Even if
    # some candidates were found, do not proceed: they may not be the best or
    # only compatible checkpoint. The caller must fail closed.
    if artifact_listing_errors:
        raise RuntimeError(
            "artifact listing failed for " + str(len(artifact_listing_errors)) + " build-stage job(s)"
        )
    return candidates


def expected_pre_download_identity(root: Path, expected_source: str) -> dict[str, str]:
    """Derive build identity from checked-out sources before any large archive download.

    GN args fingerprint is intentionally omitted here: it is stored in the output
    directory and can only be verified after restore. All source-derived identity
    fields are screened before the archive transfer.
    """
    brave = root / "src" / "brave"
    chromium = root / "src"
    if not (brave / ".git").exists() or not (chromium / ".git").exists():
        raise RuntimeError("source checkouts are unavailable for metadata pre-screening")

    def git(path: Path, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(path), *args],
            text=True, stderr=subprocess.PIPE, timeout=15,
        ).strip()

    source_commit = git(brave, "rev-parse", "HEAD")
    if source_commit != expected_source:
        raise RuntimeError(
            f"checked-out Brave source {source_commit} does not match expected {expected_source}"
        )
    chromium_commit = git(chromium, "rev-parse", "HEAD")
    chromium_version = git(chromium, "describe", "--tags", "--exact-match", "HEAD")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", chromium_version):
        raise RuntimeError(f"Chromium source tag is not a four-part numeric version: {chromium_version!r}")

    # Reuse the canonical implementation so pre-screening and build-state
    # validation calculate the same tracked-input fingerprint.
    sys.path.insert(0, str(brave / "ci" / "siso"))
    import build_state
    return {
        # Historical Brave commits may differ for CI-only changes; the canonical
        # build-input fingerprint is the compatibility gate for those changes.
        "chromium_commit": chromium_commit,
        "chromium_version": chromium_version,
        "target": "android-arm64-debug-apk",
        "build_config": "Debug",
        "build_input_fingerprint": build_state._build_input_fingerprint(),
    }


def pre_download_mismatch(meta: dict[str, Any], expected: dict[str, str]) -> list[str]:
    """Return source-derived identity mismatches before downloading an archive."""
    mismatches = [
        f"{field}: candidate={meta.get(field)!r} expected={value!r}"
        for field, value in expected.items()
        if field != "build_input_fingerprint" and meta.get(field) != value
    ]
    # Preserve build_state's narrowly-scoped timestamp-hook migration policy;
    # an exact fingerprint comparison here would reject checkpoints that the
    # canonical cache validator deliberately accepts.
    try:
        import build_state
        if not build_state._cache_input_compatible(
            meta, {"build_input_fingerprint": expected["build_input_fingerprint"]}
        ):
            mismatches.append(
                "build_input_fingerprint: candidate is incompatible with current tracked build inputs"
            )
            saved_fingerprint = meta.get("build_input_fingerprint")
            source_commit = meta.get("source_commit")
            if isinstance(saved_fingerprint, str) and isinstance(source_commit, str):
                audit = build_state._fingerprint_audit(source_commit, saved_fingerprint)
                print(
                    "CHECKPOINT_FINGERPRINT_AUDIT "
                    + json.dumps({
                        "source_commit": source_commit,
                        "saved_fingerprint": saved_fingerprint,
                        "historical_source_available": audit["historical_source_available"],
                        "historical_fingerprint": audit["historical_fingerprint"],
                        "current_fingerprint": audit["current_fingerprint"],
                        "current_with_timestamp_hook_canonicalized": audit["current_with_timestamp_hook_canonicalized"],
                        "timestamp_hook_migration_compatible": audit["timestamp_hook_migration_compatible"],
                        "audit_error": audit["audit_error"],
                    }, sort_keys=True)
                )
                for item in audit["changed_tracked_input_paths"]:
                    print(
                        "CHECKPOINT_FINGERPRINT_CHANGED_PATH "
                        + json.dumps(item, sort_keys=True)
                    )
    except Exception as exc:
        mismatches.append(
            f"build_input_fingerprint: compatibility check failed ({type(exc).__name__})"
        )
    return mismatches


def output_only_fingerprint_compatible(meta: dict[str, Any], expected: dict[str, str]) -> bool:
    """Permit output-only recovery only for the audited Siso config-only delta."""
    if not isinstance(meta, dict):
        return False
    # Every pre-download identity except the tracked-input fingerprint must match.
    if any(meta.get(key) != value for key, value in expected.items()
           if key != "build_input_fingerprint"):
        return False
    if meta.get("build_input_fingerprint") == expected.get("build_input_fingerprint"):
        return False
    source_commit = meta.get("source_commit")
    saved_fingerprint = meta.get("build_input_fingerprint")
    if not isinstance(source_commit, str) or not isinstance(saved_fingerprint, str):
        return False
    try:
        import build_state
        audit = build_state._fingerprint_audit(source_commit, saved_fingerprint)
    except Exception as exc:
        print(f"CHECKPOINT_OUTPUT_ONLY_AUDIT_ERROR={type(exc).__name__}")
        return False
    changed_paths = sorted(item.get("path") for item in audit.get("changed_tracked_input_paths", []))
    allowed = ["build/config/siso/brave_siso_config.star"]
    compatible = (
        audit.get("historical_source_available") is True
        and audit.get("historical_fingerprint") == saved_fingerprint
        and audit.get("current_fingerprint") == expected.get("build_input_fingerprint")
        and audit.get("audit_error") is None
        and changed_paths == allowed
    )
    print("CHECKPOINT_OUTPUT_ONLY_FINGERPRINT_AUDIT " + json.dumps({
        "compatible": compatible,
        "source_commit": source_commit,
        "historical_fingerprint": audit.get("historical_fingerprint"),
        "current_fingerprint": audit.get("current_fingerprint"),
        "changed_paths": changed_paths,
    }, sort_keys=True))
    return compatible


def has_siso_state_pair(meta: Any) -> bool:
    """Return whether manifest has valid Siso base state and optional journal.

    A clean Siso shutdown folds journal data into the base state and deletes
    the journal. State-only manifests therefore remain resumable; when a journal
    hash is present, it must be a valid digest too.
    """
    if not isinstance(meta, dict):
        return False
    state = meta.get("siso_state_sha256")
    journal = meta.get("siso_journal_sha256")
    state_valid = isinstance(state, str) and re.fullmatch(r"[0-9a-f]{64}", state) is not None
    journal_valid_or_absent = journal is None or (
        isinstance(journal, str) and re.fullmatch(r"[0-9a-f]{64}", journal) is not None
    )
    return state_valid and journal_valid_or_absent


def run(root: Path, *, require_siso_state: bool = False) -> int:
    root = root.resolve()
    # Never let a stale marker from an earlier invocation authorize a cold start.
    (root / ".sandium-checkpoint-discovery-status").unlink(missing_ok=True)
    # CircleCI does not provide CIRCLE_TOKEN by default. Permit common custom
    # secret names so a correctly configured project/context works without code edits.
    token = next((os.environ.get(name, "").strip() for name in
                  ("CIRCLE_TOKEN", "CIRCLECI_API_TOKEN", "CIRCLECI_TOKEN")
                  if os.environ.get(name, "").strip()), "")
    existing_local = local_checkpoint(root)
    if not token:
        if existing_local:
            print("CROSS_WORKFLOW_CHECKPOINT=SKIP_NO_TOKEN_LOCAL_CHECKPOINT_AVAILABLE")
            (root / ".sandium-checkpoint-discovery-status").write_text("DISCOVERY_INCOMPLETE\n", encoding="utf-8")
            return 0
        print("CROSS_WORKFLOW_CHECKPOINT=BLOCKED_NO_TOKEN", file=sys.stderr)
        print("Set a CircleCI personal API token as CIRCLE_TOKEN (or CIRCLECI_API_TOKEN/CIRCLECI_TOKEN) in project or context environment variables.", file=sys.stderr)
        return 78

    # Some manually triggered CircleCI pipelines omit one or more standard
    # context variables. Derive missing values from the checkout and the
    # canonical CircleCI slug rather than blocking a valid historical lookup.
    branch = os.environ.get("CIRCLE_BRANCH", "").strip()
    if not branch:
        try:
            branch = subprocess.check_output(
                ["git", "-C", str(root / "src" / "brave"), "branch", "--show-current"],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            branch = ""
    slug_candidates = []
    for candidate in (
        os.environ.get("CIRCLE_PROJECT_SLUG", "").strip(),
        os.environ.get("SANDIUM_CIRCLECI_PROJECT_SLUG", "").strip(),
        "gh/sandeepbimt/Sandium",
        "circleci/MxDP3UwnZ8Fv5ZWFv14xAJ/QEU7mKkmPf2u4opFnyaxo2",
    ):
        if candidate and candidate not in slug_candidates:
            slug_candidates.append(candidate)
    expected_source = os.environ.get("CIRCLE_SHA1", "").strip()
    if not expected_source:
        try:
            expected_source = subprocess.check_output(
                ["git", "-C", str(root / "src" / "brave"), "rev-parse", "HEAD"],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            expected_source = ""
    if not branch or not slug_candidates or not expected_source:
        if existing_local:
            print("CROSS_WORKFLOW_CHECKPOINT=SKIP_MISSING_DISCOVERY_CONTEXT_LOCAL_CHECKPOINT_AVAILABLE")
            (root / ".sandium-checkpoint-discovery-status").write_text("DISCOVERY_INCOMPLETE\n", encoding="utf-8")
            return 0
        print("CROSS_WORKFLOW_CHECKPOINT=BLOCKED_MISSING_DISCOVERY_CONTEXT", file=sys.stderr)
        print("CROSS_WORKFLOW_CHECKPOINT_CONTEXT=" +
              json.dumps({"branch_available": bool(branch), "slug_available": bool(slug_candidates),
                          "source_commit_available": bool(expected_source)}, sort_keys=True),
              file=sys.stderr)
        return 78

    root.mkdir(parents=True, exist_ok=True)
    out = root / "src" / "out" / "android_Debug_arm64"
    archive_destination = root / "sandium-checkpoint.tar.zst"
    manifest_destination = root / "sandium-checkpoint.meta"
    output_only_opt_in = os.environ.get("SANDIUM_ENABLE_OUTPUT_ONLY_RECOVERY", "").strip().lower() == "true"
    if output_only_opt_in:
        print("CHECKPOINT_OUTPUT_ONLY_RECOVERY=OPTED_IN")
    with tempfile.TemporaryDirectory(prefix="sandium-checkpoint-candidates-", dir=root) as temp_dir:
        temp = Path(temp_dir)
        # Candidate tuples carry an optional remote archive URL. Metadata is
        # small, so rank all valid manifests first and download only one large
        # archive at a time, starting with the highest-progress candidate.
        usable: list[tuple[dict[str, Any], Path, Path, str, str | None]] = []
        local_archive: Path | None = None
        local_manifest: Path | None = None
        if existing_local:
            local_meta, source_archive, source_manifest = existing_local
            local_archive, local_manifest = temp / "local-cache.tar.zst", temp / "local-cache.meta"
            shutil.copy2(source_archive, local_archive)
            shutil.copy2(source_manifest, local_manifest)
            usable.append((local_meta, local_archive, local_manifest, "local-cache", None))

        api_error: Exception | None = None
        remote_urls = []
        for candidate_slug in slug_candidates:
            try:
                remote_urls = discover_remote_urls(candidate_slug, branch, token)
                print("CROSS_WORKFLOW_CHECKPOINT_PROJECT_SLUG=resolved_candidate")
                print(f"CROSS_WORKFLOW_CHECKPOINT_BRANCH={branch}")
                print(f"CROSS_WORKFLOW_CHECKPOINT_REMOTE_ARTIFACT_PAIRS={len(remote_urls)}")
                api_error = None
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    api_error = exc
                    continue
                api_error = exc
                break
            except Exception as exc:
                api_error = exc
                break

        preflight_identity: dict[str, str] | None = None
        if not remote_urls:
            print("CROSS_WORKFLOW_CHECKPOINT_REMOTE_ARTIFACT_PAIRS=0")
            print("CHECKPOINT_PRE_DOWNLOAD_IDENTITY=NOT_RUN_NO_REMOTE_ARTIFACTS")
        if remote_urls:
            try:
                preflight_identity = expected_pre_download_identity(root, expected_source)
                print("CHECKPOINT_PRE_DOWNLOAD_IDENTITY=READY")
            except Exception as exc:
                print(f"CHECKPOINT_PRE_DOWNLOAD_IDENTITY=FAILED reason={type(exc).__name__} detail={str(exc)[:300]}", file=sys.stderr)
                print("CROSS_WORKFLOW_CHECKPOINT=DISCOVERY_INCOMPLETE; refusing archive downloads without source identity.", file=sys.stderr)
                (root / ".sandium-checkpoint-discovery-status").write_text("DISCOVERY_INCOMPLETE\\n", encoding="utf-8")
                return 78

        for index, (metadata_url, archive_url, job_number) in enumerate(remote_urls):
            metadata_path, archive_path = temp / f"remote-{index}.meta", temp / f"remote-{index}.tar.zst"
            try:
                fetch(metadata_url, metadata_path)
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if not valid_manifest(metadata):
                    print(f"CHECKPOINT_CANDIDATE_REJECTED job={job_number} reason=invalid_metadata")
                    continue
                if preflight_identity is not None:
                    mismatches = pre_download_mismatch(metadata, preflight_identity)
                    if mismatches:
                        if (output_only_opt_in
                                and all(item.startswith("build_input_fingerprint:") for item in mismatches)
                                and output_only_fingerprint_compatible(metadata, preflight_identity)):
                            metadata["_sandium_output_only_recovery"] = True
                            print(f"CHECKPOINT_CANDIDATE_ACCEPTED_OUTPUT_ONLY job={job_number} reason=audited_siso_config_only_delta")
                        else:
                            print(f"CHECKPOINT_CANDIDATE_REJECTED job={job_number} reason=pre_download_identity_mismatch")
                            for mismatch in mismatches:
                                print(f"CHECKPOINT_CANDIDATE_IDENTITY_MISMATCH job={job_number} {mismatch}")
                            continue
                has_siso_pair = has_siso_state_pair(metadata)
                is_output_only_candidate = bool(metadata.get("_sandium_output_only_recovery"))
                if require_siso_state and not has_siso_pair and not (
                    output_only_opt_in and is_output_only_candidate
                ):
                    print(f"CHECKPOINT_CANDIDATE_REJECTED job={job_number} reason=missing_or_invalid_siso_state_metadata")
                    continue
                usable.append((metadata, archive_path, metadata_path, job_number, archive_url))
            except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
                print(f"CHECKPOINT_CANDIDATE_SKIPPED job={job_number} reason={type(exc).__name__}")

        if api_error:
            print(f"CROSS_WORKFLOW_CHECKPOINT=API_ERROR_FALLBACK reason={type(api_error).__name__}")
        usable.sort(key=lambda item: rank(item[0]), reverse=True)
        if require_siso_state:
            output_only_candidates = [
                item for item in usable
                if output_only_opt_in and item[0].get("_sandium_output_only_recovery")
            ]
            resumable = [item for item in usable if has_siso_state_pair(item[0])]
            output_only_count = len(output_only_candidates)
            print(f"CROSS_WORKFLOW_CHECKPOINT_CANDIDATES_TOTAL={len(usable)}")
            print(f"CROSS_WORKFLOW_CHECKPOINT_CANDIDATES_RESUMABLE={len(resumable)}")
            print(f"CROSS_WORKFLOW_CHECKPOINT_CANDIDATES_OUTPUT_ONLY={output_only_count}")
            usable = sorted(
                resumable + output_only_candidates,
                key=lambda item: rank(item[0]),
                reverse=True,
            )
            if not usable and api_error:
                print("CROSS_WORKFLOW_CHECKPOINT=DISCOVERY_INCOMPLETE; refusing to conclude no resumable checkpoint exists.", file=sys.stderr)
                print("CROSS_WORKFLOW_CHECKPOINT=API_ERROR_FALLBACK reason=" + type(api_error).__name__, file=sys.stderr)
                (root / ".sandium-checkpoint-discovery-status").write_text(
                    "DISCOVERY_INCOMPLETE\n", encoding="utf-8"
                )
                return 78
            if not usable:
                print("CROSS_WORKFLOW_CHECKPOINT=NO_RESUMABLE_SISO_CANDIDATE")
                (root / ".sandium-checkpoint-discovery-status").write_text(
                    "NO_RESUMABLE_SISO_CANDIDATE\n", encoding="utf-8"
                )
                return 0
        if not usable:
            print("CROSS_WORKFLOW_CHECKPOINT=NO_COMPATIBLE_CANDIDATE")
            if api_error:
                print("CROSS_WORKFLOW_CHECKPOINT=DISCOVERY_INCOMPLETE; refusing to authorize a cold start.", file=sys.stderr)
                print("Set/check the CircleCI personal API token and permissions before retrying.", file=sys.stderr)
                return 78
            # A fresh Stage 1 build is safe only after historical checkpoint
            # discovery completed successfully and found no compatible candidate.
            (root / ".sandium-checkpoint-discovery-status").write_text("NO_COMPATIBLE_CANDIDATE\n", encoding="utf-8")
            return 0

        # Preserve any pre-existing output while candidates are tried. A corrupt
        # or incompatible top-ranked archive must not destroy a usable local cache.
        output_backup = temp / "preexisting-output"
        had_output = out.exists() or out.is_symlink()
        if had_output:
            output_backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(out, output_backup)

        succeeded = False
        try:
            checkpoint_script = root / "src" / "brave" / "ci" / "siso" / "checkpoint.py"
            state_script = root / "src" / "brave" / "ci" / "siso" / "build_state.py"
            for metadata, archive_path, metadata_path, job_number, archive_url in usable:
                if archive_url is not None:
                    try:
                        print(f"CHECKPOINT_CANDIDATE_ARCHIVE_DOWNLOAD_START job={job_number} bytes={metadata.get('archive_bytes', 'unknown')}")
                        fetch(archive_url, archive_path)
                    except (OSError, ValueError, urllib.error.URLError) as exc:
                        print(f"CHECKPOINT_CANDIDATE_SKIPPED job={job_number} reason=archive_download_{type(exc).__name__}")
                        archive_path.unlink(missing_ok=True)
                        continue
                    if not archive_integrity_matches(metadata, archive_path):
                        print(f"CHECKPOINT_CANDIDATE_REJECTED job={job_number} reason=archive_integrity")
                        archive_path.unlink(missing_ok=True)
                        continue
                output_only_recovery = metadata.pop("_sandium_output_only_recovery", False)
                shutil.copy2(archive_path, archive_destination)
                shutil.copy2(metadata_path, manifest_destination)
                restore_command = [sys.executable, str(checkpoint_script),
                                   "restore-output-only" if output_only_recovery else "restore",
                                   "--root", str(root)]
                if output_only_recovery:
                    restore_command.extend(["--commit", expected_source])
                restored = subprocess.run(
                    restore_command,
                    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                if "SANDIUM_CHECKPOINT_RESTORE=PASS" not in restored.stdout:
                    # Preserve the actual validator reason in CI logs. The restore
                    # script emits controlled diagnostics (not credentials); only
                    # its final few non-empty lines are surfaced to avoid log floods.
                    detail_lines = [line.strip() for line in restored.stdout.splitlines() if line.strip()]
                    detail = " | ".join(detail_lines[-4:])[:700] or "no_restore_diagnostic"
                    print(f"CHECKPOINT_CANDIDATE_REJECTED job={job_number} reason=restore_integrity detail={detail}")
                    shutil.rmtree(out, ignore_errors=True)
                    continue
                if output_only_recovery:
                    print(f"CROSS_WORKFLOW_CHECKPOINT=RESTORED_OUTPUTS_ONLY job={job_number} stage={metadata.get('stage')}")
                    print("CROSS_WORKFLOW_CHECKPOINT_PROGRESS=" + json.dumps(metadata["progress"], sort_keys=True))
                    print("CROSS_WORKFLOW_CHECKPOINT_SISO_STATE=DISCARDED")
                    print("SANDIUM_COMPILE_AUTHORIZATION=NOT_GRANTED")
                    (root / ".sandium-checkpoint-discovery-status").write_text(
                        "RESTORED_OUTPUTS_ONLY\n", encoding="utf-8"
                    )
                    succeeded = True
                    return 0
                check = subprocess.run(
                    [sys.executable, str(state_script), "check", "--mode", "cache", "--commit", expected_source],
                    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                if check.returncode == 0:
                    print(f"CROSS_WORKFLOW_CHECKPOINT=RESTORED job={job_number} stage={metadata.get('stage')}")
                    print("CROSS_WORKFLOW_CHECKPOINT_PROGRESS=" + json.dumps(metadata["progress"], sort_keys=True))
                    (root / ".sandium-checkpoint-discovery-status").write_text("RESTORED\n", encoding="utf-8")
                    succeeded = True
                    return 0
                print(f"CROSS_WORKFLOW_CHECKPOINT=INCOMPATIBLE job={job_number}; trying next candidate")
                shutil.rmtree(out, ignore_errors=True)
        finally:
            if not succeeded:
                shutil.rmtree(out, ignore_errors=True)
                if had_output and (output_backup.exists() or output_backup.is_symlink()):
                    out.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(output_backup, out)
                # Restore the original cache pair so the next pipeline step may
                # use it as a safe fallback; otherwise remove the last bad candidate.
                if existing_local and local_archive and local_manifest:
                    shutil.copy2(local_archive, archive_destination)
                    shutil.copy2(local_manifest, manifest_destination)
                else:
                    archive_destination.unlink(missing_ok=True)
                    manifest_destination.unlink(missing_ok=True)

        print("CROSS_WORKFLOW_CHECKPOINT=RESTORE_FAILED; refusing to authorize a cold start.", file=sys.stderr)
        # We found at least one apparently usable candidate but none restored
        # and passed compatibility checks. This is not equivalent to a completed
        # search with no compatible candidate; preserve state and fail closed.
        return 78


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("CIRCLE_WORKING_DIRECTORY") or os.getcwd()),
        help="CircleCI project root (defaults to the current working directory)",
    )
    parser.add_argument(
        "--require-siso-state", action="store_true",
        help="Only restore checkpoints whose metadata includes both Siso state digests.",
    )
    args = parser.parse_args()
    # CircleCI environment variables may contain a literal ~/ prefix; expand it
    # before resolving so paths never become <cwd>/~/project.
    root = Path(os.path.expandvars(str(args.root))).expanduser().resolve()
    return run(root, require_siso_state=args.require_siso_state)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("CROSS_WORKFLOW_CHECKPOINT=API_ERROR " + str(exc), file=sys.stderr)
        raise SystemExit(78)
