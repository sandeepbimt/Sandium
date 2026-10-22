#!/usr/bin/env python3
"""Lightweight CircleCI token/artifact-discovery probe; never downloads artifacts."""
from __future__ import annotations

import json
import os
import sys
import subprocess
import urllib.error
import urllib.parse
import urllib.request

API = "https://circleci.com/api/v2"


def get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url, headers={"Circle-Token": token, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CircleCI returned a non-object response")
    return value


def pages(url: str, token: str, key: str):
    while url:
        value = get_json(url, token)
        items = value.get(key, [])
        if isinstance(items, list):
            yield from (item for item in items if isinstance(item, dict))
        next_page = value.get("next_page_token")
        if not next_page:
            break
        separator = "&" if "?" in url else "?"
        url = url + separator + urllib.parse.urlencode({"page-token": next_page})


def main() -> int:
    token = next((os.environ.get(name, "").strip() for name in
                  ("CIRCLE_TOKEN", "CIRCLECI_API_TOKEN", "CIRCLECI_TOKEN")
                  if os.environ.get(name, "").strip()), "")
    slug = os.environ.get("CIRCLE_PROJECT_SLUG", "").strip()
    if not slug:
        # Prefer the canonical VCS owner/repository from CircleCI's repository
        # URL. CIRCLE_PROJECT_USERNAME can be an email/account label and is
        # not guaranteed to be the GitHub owner segment.
        repository_url = os.environ.get("CIRCLE_REPOSITORY_URL", "").strip()
        if repository_url:
            parsed = urllib.parse.urlparse(repository_url)
            path = parsed.path.strip("/")
            if parsed.hostname and parsed.hostname.lower() in ("github.com", "www.github.com"):
                path = path[:-4] if path.lower().endswith(".git") else path
                parts = path.split("/")
                if len(parts) >= 2 and all(parts[:2]):
                    slug = f"gh/{parts[0]}/{parts[1]}"
        if not slug:
            username = os.environ.get("CIRCLE_PROJECT_USERNAME", "").strip()
            reponame = os.environ.get("CIRCLE_PROJECT_REPONAME", "").strip()
            if username and reponame and "@" not in username:
                slug = f"gh/{username}/{reponame}"
    branch = os.environ.get("CIRCLE_BRANCH", "").strip()
    if not branch:
        # Manual/API-triggered pipelines may omit CIRCLE_BRANCH even though
        # checkout has selected the configured branch.
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
    if not slug:
        print("CHECKPOINT_PROBE=FAIL reason=project_slug_missing")
        return 2
    if not branch:
        print("CHECKPOINT_PROBE=FAIL reason=branch_context_missing")
        return 2

    try:
        # Validate the token independently from project lookup so a project
        # slug/visibility 404 cannot be misreported as an authentication issue.
        get_json(f"{API}/me", token)
        project = urllib.parse.quote(slug, safe="/")
        pipeline_url = f"{API}/project/{project}/pipeline?branch={urllib.parse.quote(branch)}&page-size=20"
        workflows_seen = 0
        build_jobs_seen = 0
        checkpoint_pairs_seen = 0
        for pipeline in pages(pipeline_url, token, "items"):
            pipeline_id = pipeline.get("id")
            if not pipeline_id:
                continue
            workflow_url = f"{API}/pipeline/{urllib.parse.quote(str(pipeline_id), safe='')}/workflow"
            for workflow in pages(workflow_url, token, "items"):
                workflow_id = workflow.get("id")
                if not workflow_id:
                    continue
                workflows_seen += 1
                jobs_url = f"{API}/workflow/{urllib.parse.quote(str(workflow_id), safe='')}/job"
                for job in pages(jobs_url, token, "items"):
                    name = str(job.get("name", ""))
                    number = job.get("job_number")
                    if not name.startswith(("stage-1-", "stage-2-", "stage-3-", "stage-4-", "stage-5-")) or not number:
                        continue
                    build_jobs_seen += 1
                    artifacts_url = f"{API}/project/{project}/{number}/artifacts"
                    paths = {str(item.get("path", "")) for item in pages(artifacts_url, token, "items")}
                    has_meta = any(path.endswith("checkpoint-artifact/sandium-checkpoint.meta") for path in paths)
                    has_archive = any(path.endswith("checkpoint-artifact/sandium-checkpoint.tar.zst") for path in paths)
                    if has_meta and has_archive:
                        checkpoint_pairs_seen += 1
        print("CHECKPOINT_PROBE=PASS")
        print(f"CHECKPOINT_PROBE_WORKFLOWS_SEEN={workflows_seen}")
        print(f"CHECKPOINT_PROBE_BUILD_JOBS_SEEN={build_jobs_seen}")
        print(f"CHECKPOINT_PROBE_ARTIFACT_PAIRS_SEEN={checkpoint_pairs_seen}")
        print("CHECKPOINT_PROBE_ARTIFACTS_DOWNLOADED=0")
        return 0
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            reason = "auth_or_permission"
        elif exc.code == 404:
            reason = "project_or_api_route_not_found"
        else:
            reason = f"http_{exc.code}"
        print(f"CHECKPOINT_PROBE=FAIL reason={reason}")
        return 3
    except (urllib.error.URLError, TimeoutError):
        print("CHECKPOINT_PROBE=FAIL reason=network_or_timeout")
        return 4
    except (ValueError, json.JSONDecodeError):
        print("CHECKPOINT_PROBE=FAIL reason=invalid_api_response")
        return 5
    except Exception as exc:
        # Report only the exception class; never print request headers or secret data.
        print(f"CHECKPOINT_PROBE=FAIL reason={type(exc).__name__}")
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
