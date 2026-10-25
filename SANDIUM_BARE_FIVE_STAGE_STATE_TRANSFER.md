# Sandium Bare: Five-Stage State Transfer Design

Status: **design review only — not build-ready**

This document defines the state-transfer contract for the Bare-only Android ARM64 Debug APK workflow. It does not enable a pipeline, sync Chromium, run GN, compile, package an APK, upload large artifacts, or alter credentials/caches.

## Pinned build identity

- Bare repository: https://github.com/BareBrowser/bare-browser.git
- Bare commit: `a5a7ef74f3b77d45720b8c502cbacc30f2f3dbd5`
- Chromium commit: `1228231501697ff7a4a0150ea8ae6df37bb99331`
- Chromium version: `156.0.8078.25`
- Target: Android ARM64, Debug, minimum Android 10
- Expected APK: `Sandium-debug-arm64.apk`

## Five-stage contract

| Stage | Responsibility | Required hand-off/evidence |
|---|---|---|
| 1 — source-foundation | Validate Bare pin, patch-series digest, uBlock CRX checksum, Chromium base pin and patch application plan | Immutable build identity record; no claim that a checkout exists unless verified |
| 2 — toolchain-and-graph | Verify depot_tools/Siso identity, effective GN args and generated build graph | Same build identity hash; GN args and toolchain evidence |
| 3 — compile-and-ledger | Perform incremental compilation with `use_remoteexec=true`, `use_siso=true`, `use_reclient=false` | Siso metrics/logs proving at least one actual remote-executed action; stage ledger |
| 4 — restore-and-continue | Restore only a verified matching checkpoint, then continue incremental work if needed | Same build identity; previous ledger; cache hit evidence OR explicit, measured no-work result |
| 5 — finalize-and-publish | Complete remaining build work, verify APK structure/signature, emit manifest and SHA-256 | Same identity; signed APK verification; manifest; checksum |

A stage may report “no work” only when evidence shows there are no remaining actions for the same build identity. An empty or missing metrics file is not proof of no work. Remote execution must be proven from Siso metrics/logs; GN flags alone do not prove that actions ran remotely. Cache reuse must be proven from metrics/logs; the existence of a cache configuration alone is not proof of a cache hit.

## State that must survive stage boundaries

1. **Source state:** exact Chromium base plus the complete ordered Bare patch series, Bare checkout and pinned uBlock CRX.
2. **Build state:** generated GN files, toolchain/dependency state and the complete `src/out/android_Debug_arm64` output tree required for incremental continuation.
3. **Evidence state:** source/build identity, Siso logs/metrics, stage ledger, checkpoint metadata and hashes.
4. **Artifact state:** final APK and manifest only after verification.

The metadata ledger is small, but it does **not** preserve the Chromium checkout or build output. The existing `ci/bare/checkpoint.py` can package/restore the output directory locally with identity checks; it does not provide a durable cross-pipeline storage service.

## State-transfer decision and blocker

CircleCI workspaces can hand off files between jobs in the same workflow, but that fact alone does not establish that a multi-gigabyte Chromium output archive is affordable, within account/storage limits, or suitable for repeated five-stage transfers. Hosted job containers are not durable storage across separate pipelines. Siso remote action cache is not a substitute for the source checkout, generated GN state, or all local build outputs.

No persistent runner or approved external checkpoint store has been verified for this project. Therefore this design **does not** assume that:
- CircleCI workspaces can hold and transfer the entire checkout/output tree within the account's limits;
- a persistent runner is available on the current plan;
- BuildBuddy/Siso action cache can restore all required local state;
- a checkpoint has been uploaded, retained, or successfully restored.

Until a supported state store is identified and its limits/retention/access are verified without uploading a large checkpoint, the full-build switch must remain false and the existing fail-closed job must remain in place.

## Non-negotiable safety gates

- Keep `run_sandium_bare_source_preflight=false`, `run_sandium_bare_source_ready=false`, and `run_sandium_bare_android_apk_build=false` by default.
- Never delete an existing cache, output tree, or checkpoint to force a cold run.
- Never overwrite an existing restore backup; stop and require review if one already exists.
- Reject any checkpoint whose source, patch, extension, toolchain, GN or build identity differs.
- Fail before Chromium sync/compilation when the persistence mechanism or required credentials are absent.
- Do not claim remote execution, cache reuse, or APK success without captured evidence.
- Fast documentation/source-only checks and full Chromium builds must remain separate paths. Any future pipeline/job name should make the expected cost obvious (for example, `FAST-Preflight` versus `LONG-Full-APK`).

## Readiness checklist

The full build remains blocked until all are true:

- [ ] The actual CircleCI plan and its applicable workspace/storage limits are confirmed.
- [ ] An approved mechanism can preserve/restore the required source and output state across stages without exceeding known limits.
- [ ] The state-transfer mechanism is tested on a small harmless fixture first, not a Chromium tree.
- [ ] Stage-ledger semantics align with the five-stage contract: stages 1–2 are preparation/configuration; stage 3 proves real remote execution; stage 4 proves cache reuse or measured no-work; stage 5 verifies the artifact.
- [ ] The workflow validates identity before restore and after restore, and preserves any pre-existing output.
- [ ] A separately approved, budget-aware build run is explicitly requested.

## Current outcome

This is a reviewable state-transfer contract, **not a runnable or build-ready five-stage pipeline**. No Chromium sync, GN generation, compilation, APK packaging, remote-execution test, checkpoint upload, or CI pipeline trigger was performed to create this document.
