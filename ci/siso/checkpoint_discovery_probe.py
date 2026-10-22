#!/usr/bin/env python3
"""Lightweight CircleCI token/artifact-discovery probe; never downloads artifacts."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

API = "https://circleci.com/api/v2"
DEFAULT_PROJECT_SLUG = "gh/sandeepbimt/Sandium"
CURRENT_ENDPOINT = "not_started"


def get_json(url: str, token: str, endpoint: str) -> dict:
    global CURRENT_ENDPOINT
    CURRENT_ENDPOINT = endpoint
    request = urllib.request.Request(
        url, headers={"Circle-Token": token, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CircleCI returned a non-object response")
    return value


def pages(url: str, token: str, key: str, endpoint: str):
    while url:
        value = get_json(url, token, endpoint)
        items = value.get(key, [])
        if isinstance(items, list):
            yield from (item for item in items if isinstance(item, dict))
        next_page = value.get("next_page_token")
        if not next_page:
            break
        separator = "&" if "?" in url else "?"
        url = url + separator + urllib.parse.urlencode({"page-token": next_page})


def main() -> int:
    global CURRENT_ENDPOINT
    token = next((os.environ.get(name, "").strip() for name in
                  ("CIRCLE_TOKEN", "CIRCLECI_API_TOKEN", "CIRCLECI_TOKEN")
                  if os.environ.get(name, "").strip()), "")
    slug = os.environ.get("SANDIUM_CIRCLECI_PROJECT_SLUG", "").strip() or DEFAULT_PROJECT_SLUG
    branch = os.environ.get("CIRCLE_BRANCH", "").strip()
    if not branch:
        try:
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            branch = ""
    if not token:
        print("CHECKPOINT_PROBE=FAIL reason=token_missing")
        return 2
    if not branch:
        print("CHECKPOINT_PROBE=FAIL reason=branch_context_missing")
        return 2

    try:
        get_json(f"{API}/me", token, "token_validation_me")
        project = urllib.parse.quote(slug, safe="/")
        pipeline_url = (f"{API}/project/{project}/pipeline?"
                        f"branch={urllib.parse.quote(branch)}&page-size=20")
        workflows_seen = build_jobs_seen = checkpoint_pairs_seen = 0
        artifact_404s = artifact_lookups = 0
        for pipeline in pages(pipeline_url, token, "items", "project_pipelines"):
            pipeline_id = pipeline.get("id")
            if not pipeline_id:
                continue
            workflow_url = f"{API}/pipeline/{urllib.parse.quote(str(pipeline_id), safe='')}/workflow"
            for workflow in pages(workflow_url, token, "items", "pipeline_workflows"):
                workflow_id = workflow.get("id")
                if not workflow_id:
                    continue
                workflows_seen += 1
                jobs_url = f"{API}/workflow/{urllib.parse.quote(str(workflow_id), safe='')}/job"
                for job in pages(jobs_url, token, "items", "workflow_jobs"):
                    name = str(job.get("name", ""))
                    number = job.get("job_number")
                    if not name.startswith(("stage-1-", "stage-2-", "stage-3-", "stage-4-", "stage-5-")) or not number:
                        continue
                    build_jobs_seen += 1
                    artifact_slugs = []
                    job_slug = str(job.get("project_slug", "")).strip()
                    for candidate in (job_slug, slug):
                        if candidate and candidate not in artifact_slugs:
                            artifact_slugs.append(candidate)
                    paths = set()
                    found = False
                    for artifact_slug in artifact_slugs:
                        artifact_lookups += 1
                        artifact_project = urllib.parse.quote(artifact_slug, safe="/")
                        artifacts_url = f"{API}/project/{artifact_project}/{number}/artifacts"
                        try:
                            paths = {str(item.get("path", "")) for item in
                                     pages(artifacts_url, token, "items", "job_artifacts")}
                            found = True
                            break
                        except urllib.error.HTTPError as exc:
                            if exc.code == 404:
                                artifact_404s += 1
                                continue
                            raise
                    if not found:
                        continue
                    has_meta = any(path.endswith("checkpoint-artifact/sandium-checkpoint.meta") for path in paths)
                    has_archive = any(path.endswith("checkpoint-artifact/sandium-checkpoint.tar.zst") for path in paths)
                    if has_meta and has_archive:
                        checkpoint_pairs_seen += 1

        # A 404 for an individual historical job can simply mean that the job
        # has no retained artifacts. API enumeration is healthy if auth and
        # pipeline/workflow/job endpoints succeeded. Do not fail the probe for
        # expected misses when usable checkpoint pairs were found.
        if checkpoint_pairs_seen:
            print("CHECKPOINT_PROBE=PASS")
        else:
            print("CHECKPOINT_PROBE=PARTIAL reason=no_checkpoint_pairs_found")
        print(f"CHECKPOINT_PROBE_WORKFLOWS_SEEN={workflows_seen}")
        print(f"CHECKPOINT_PROBE_BUILD_JOBS_SEEN={build_jobs_seen}")
        print(f"CHECKPOINT_PROBE_ARTIFACT_PAIRS_SEEN={checkpoint_pairs_seen}")
        print(f"CHECKPOINT_PROBE_ARTIFACT_LOOKUPS={artifact_lookups}")
        print(f"CHECKPOINT_PROBE_ARTIFACT_LOOKUP_404S={artifact_404s}")
        print("CHECKPOINT_PROBE_ARTIFACTS_DOWNLOADED=0")
        return 0 if checkpoint_pairs_seen else 7
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            reason = "auth_or_permission"
        elif exc.code == 404:
            reason = "not_found"
        else:
            reason = f"http_{exc.code}"
        print(f"CHECKPOINT_PROBE=FAIL reason={reason} endpoint={CURRENT_ENDPOINT}")
        return 3
    except (urllib.error.URLError, TimeoutError):
        print(f"CHECKPOINT_PROBE=FAIL reason=network_or_timeout endpoint={CURRENT_ENDPOINT}")
        return 4
    except (ValueError, json.JSONDecodeError):
        print(f"CHECKPOINT_PROBE=FAIL reason=invalid_api_response endpoint={CURRENT_ENDPOINT}")
        return 5
    except Exception as exc:
        print(f"CHECKPOINT_PROBE=FAIL reason={type(exc).__name__} endpoint={CURRENT_ENDPOINT}")
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
