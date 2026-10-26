#!/usr/bin/env python3
"""Verify real Siso remote execution/cache evidence from logs and metrics.

The parser accepts both Chromium/Siso structured action records and the summary
format observed in the successful SANDFOX/Afterbird Android build. It does not
infer success from GN flags or from an APK merely existing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Example from SANDFOX run 38078381386:
# local:16660 remote:16674 cache:30278 cache-write:0(err:0) fallback:0 retry:0 skip:34067
SUMMARY_RE = re.compile(
    r"\blocal:(\d+)\s+remote:(\d+)\s+cache:(\d+)\b", re.IGNORECASE
)
REMOTE_JSON_RE = re.compile(
    r'"(?:is_remote|remote_execution|remoteExecution)"\s*:\s*true|'
    r'"is_local"\s*:\s*false|step state:\s*remote exec|exec remote execute',
    re.IGNORECASE,
)
CACHE_JSON_RE = re.compile(
    r'"(?:cached|cache_hit|cacheHit|remote_action_cache_hit)"\s*:\s*true',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Evidence:
    remote_actions: int = 0
    cache_hits: int = 0
    remote_event_records: int = 0
    cache_event_records: int = 0

    @property
    def remote_proven(self) -> bool:
        return self.remote_actions > 0 or self.remote_event_records > 0

    @property
    def cache_reuse_proven(self) -> bool:
        return self.cache_hits > 0 or self.cache_event_records > 0


def _walk_json(value: Any) -> tuple[int, int, int, int]:
    """Count explicitly typed action evidence in a JSON object."""
    remote_actions = cache_hits = remote_events = cache_events = 0
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = re.sub(r"[^a-z0-9]", "", str(raw_key).lower())
            if key in {"isremote", "remoteexecution", "remotelyexecuted"} and item is True:
                remote_events += 1
            elif key == "islocal" and item is False:
                remote_events += 1
            elif key in {"cached", "cachehit", "fromcache", "remoteactioncachehit"} and item is True:
                cache_events += 1
            elif key in {"remoteactions", "remoteactioncount", "remotecount"} and isinstance(item, int):
                remote_actions += max(0, item)
            elif key in {"cachehits", "cachehitcount", "remoteactioncachehits"} and isinstance(item, int):
                cache_hits += max(0, item)
            r, c, revents, cevents = _walk_json(item)
            remote_actions += r
            cache_hits += c
            remote_events += revents
            cache_events += cevents
    elif isinstance(value, list):
        for item in value:
            r, c, revents, cevents = _walk_json(item)
            remote_actions += r
            cache_hits += c
            remote_events += revents
            cache_events += cevents
    return remote_actions, cache_hits, remote_events, cache_events


def parse_text(text: str) -> Evidence:
    remote_actions = cache_hits = remote_events = cache_events = 0
    for line in text.splitlines():
        summary = SUMMARY_RE.search(line)
        if summary:
            # Logs can contain multiple summaries; keep the largest observed
            # counts instead of summing snapshots and double-counting them.
            remote_actions = max(remote_actions, int(summary.group(2)))
            cache_hits = max(cache_hits, int(summary.group(3)))

        if REMOTE_JSON_RE.search(line):
            remote_events += 1
        if CACHE_JSON_RE.search(line):
            cache_events += 1

        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                item = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            r, c, revents, cevents = _walk_json(item)
            remote_actions += r
            cache_hits += c
            remote_events += revents
            cache_events += cevents

    return Evidence(remote_actions, cache_hits, remote_events, cache_events)


def inspect_files(paths: list[Path]) -> Evidence:
    evidence = Evidence()
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            chunk = parse_text(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        # Multiple files may carry the same final summary; take maxima for
        # totals while preserving event counts across files.
        evidence = Evidence(
            remote_actions=max(evidence.remote_actions, chunk.remote_actions),
            cache_hits=max(evidence.cache_hits, chunk.cache_hits),
            remote_event_records=evidence.remote_event_records + chunk.remote_event_records,
            cache_event_records=evidence.cache_event_records + chunk.cache_event_records,
        )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, action="append", default=[])
    parser.add_argument("--metrics", type=Path, action="append", default=[])
    parser.add_argument("--require-remote", action="store_true",
                        help="exit nonzero unless at least one remote action is positively evidenced")
    parser.add_argument("--require-cache", action="store_true",
                        help="exit nonzero unless at least one cache hit is positively evidenced")
    args = parser.parse_args()
    evidence = inspect_files(args.log + args.metrics)

    print(f"SISO_REMOTE_ACTIONS={evidence.remote_actions}")
    print(f"SISO_CACHE_HITS={evidence.cache_hits}")
    print(f"SISO_REMOTE_EVENT_RECORDS={evidence.remote_event_records}")
    print(f"SISO_CACHE_EVENT_RECORDS={evidence.cache_event_records}")
    print("SISO_REMOTE_EXECUTION=" + ("PASS" if evidence.remote_proven else "UNPROVEN"))
    print("SISO_REMOTE_ACTION_CACHE_REUSE=" + ("PASS" if evidence.cache_reuse_proven else "NO_HITS_OR_UNPROVEN"))

    failed = False
    if args.require_remote and not evidence.remote_proven:
        print("SISO_EVIDENCE_ERROR=No positive remote execution evidence in final Siso logs/metrics", file=sys.stderr)
        failed = True
    if args.require_cache and not evidence.cache_reuse_proven:
        print("SISO_EVIDENCE_ERROR=No positive remote action-cache hit evidence in final Siso logs/metrics", file=sys.stderr)
        failed = True
    return 42 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
