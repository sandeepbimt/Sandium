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

## Reference implementation reviewed

Reference: [sandeepbimt/iceraven-browser, branch `sandfox/checkpoint-stage-v1`, commit `2b254b9`](https://github.com/sandeepbimt/iceraven-browser/tree/sandfox/checkpoint-stage-v1), specifically `.circleci/config.yml`.

The reference provides a concrete staged-checkpoint pattern worth adapting:

- Stage-specific cache keys use the CircleCI workflow ID, so stages in one workflow can restore the preceding stage's checkpoint.
- The checkpoint archive and metadata are saved as cache paths, separately from the small APK-completion marker.
- A later stage checks the completion marker early and halts when a previous stage has already recorded a ZIP-verified APK.
- The checkpoint self-test checks archive round-tripping for compiled outputs and Siso state/journal files, including timestamp preservation.
- The stage gate rejects a timed-out build if it did not prove remote work and useful progress; it does not blindly proceed through all five stages.

**Evidence boundary:** the supplied screenshot shows the source for this reference commit and a CircleCI run marked **Canceled**. The configuration demonstrates the mechanism and its intended checks; the screenshot alone does not prove that the shown run completed all five stages or produced an APK. We must inspect the relevant successful run's logs/artifacts before claiming those outcomes.

## Five-stage contract

| Stage | Responsibility | Required hand-off/evidence |
|---|---|---|
| 1 — source-foundation | Validate Bare pin, patch-series digest, uBlock CRX checksum, Chromium base pin and patch application plan | Immutable build identity record; no claim that a checkout exists unless verified |
| 2 — toolchain-and-graph | Verify depot_tools/Siso identity, effective GN args and generated build graph | Same build identity hash; GN args and toolchain evidence |
| 3 — compile-and-ledger | Perform incremental compilation with `use_remoteexec=true`, `use_siso=true`, `use_reclient=false` | Siso metrics/logs proving at least one actual remote-executed action; stage ledger |
| 4 — restore-and-continue | Restore only a verified matching checkpoint, then continue incremental work if needed | Same build identity; previous ledger; cache hit evidence OR explicit, measured no-work result |
| 5 — finalize-and-publish | Complete remaining build work, verify APK structure/signature, emit manifest and SHA-256 | Same identity; signed APK verification; manifest; checksum |

A stage may report “no work” only when evidence shows there are no remaining actions for the same build identity. An empty or missing metrics file is not proof of no work. Remote execution must be proven from Siso metrics/logs; GN flags alone do not prove that actions ran remotely. Cache reuse must be proven from metrics/logs; the existence of a cache configuration alone is not proof of a cache hit.

## Adapt the reference, do not copy it blindly

1. **Reuse the checkpoint idea:** package the output tree and Siso state/journal together with a metadata file that records build identity, archive digest, state mode and stage progress.
2. **Use immutable, workflow-scoped stage keys:** each stage restores only the expected preceding stage's checkpoint; a new workflow may restore a prior checkpoint only after validating the same pinned source/build identity. Never overwrite a cache key expecting it to mutate—CircleCI caches are immutable once saved.
3. **Keep an independent completion marker:** only write `BUILD_FINISHED=true` after APK validation and a checksum are recorded. Later stages may fast-exit only after validating the marker and the artifact evidence available to them.
4. **Preserve the Siso state files:** checkpoint archive must include `.siso_fs_state` and/or the valid journal recovery state, as well as generated build graph, GN args and compiled output. Validate archive round-trip behavior on a harmless fixture before using it for a real build.
5. **Keep source reconstruction separate from build-output restoration:** the reference config reconstructs the Chromium source in later stages while restoring the output checkpoint. For Sandium, that source must be pinned and patch identity revalidated before accepting the restored output; source sync must not be repeated casually if a supported source hand-off is available.
6. **Bound each stage and require progress:** a stage that times out without an APK must save a verified checkpoint and show new completed actions/output, with remote-execution evidence. If it cannot prove useful progress, stop rather than paying for another repeated stage.
7. **Keep full-build and fast checks separate:** fast preflight/documentation checks must never invoke Chromium sync or compilation.

## State that must survive stage boundaries

1. **Source state:** exact Chromium base plus the complete ordered Bare patch series, Bare checkout and pinned uBlock CRX.
2. **Build state:** generated GN files, toolchain/dependency state and the complete `src/out/android_Debug_arm64` output tree required for incremental continuation.
3. **Siso state:** `.siso_fs_state` and/or a valid recoverable journal, preserved with graph/output files and tested after extraction.
4. **Evidence state:** source/build identity, Siso logs/metrics, stage ledger, checkpoint metadata and hashes.
5. **Artifact state:** final APK and manifest only after verification.

The metadata ledger is small, but it does **not** preserve the Chromium checkout or build output. The existing `ci/bare/checkpoint.py` can package/restore the output directory locally with identity checks; it does not by itself implement CircleCI cache save/restore or prove that Siso state survives a real stage transition.

## State-transfer decision and blocker

The reference makes a **specific implementation path** available to investigate: use CircleCI's stage-scoped `save_cache`/`restore_cache` pattern for the checkpoint archive and metadata, plus a separate APK completion marker. This is more concrete than assuming a persistent runner or sending a full output tree through a workspace at every stage.

However, it still needs validation against the current Sandium workflow and account:
- The reference archive's actual size and cache retention/size behavior for the relevant run have not been verified here.
- The current CircleCI plan's applicable cache limits and retention need confirmation.
- CircleCI caches are immutable; stage keys must be unique and correctly scoped.
- A cache restore miss must fail closed or use an explicitly approved recovery path; it must not silently trigger an expensive clean sync/build.
- BuildBuddy/Siso action cache is not a substitute for the source checkout, generated GN state, or all local build outputs.
- The screenshot shows a canceled run, so it is not evidence of end-to-end success.

Therefore this design does **not** claim that a checkpoint has been uploaded, retained, restored, or successfully continued in Sandium. Keep the full-build switch disabled until a harmless archive self-test, cache hand-off validation, identity gates and current-plan limits have been reviewed.

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

- [ ] Inspect a successful reference run's logs/artifacts and record its actual checkpoint sizes, stage progress and APK verification result.
- [ ] Confirm the current CircleCI plan's cache limits and retention behavior.
- [ ] Adapt the reference's cache key design for Sandium's pinned source identity and immutable cache semantics.
- [ ] Test archive round-trip, including Siso state/journal and file metadata, on a small harmless fixture.
- [ ] Align stage-ledger semantics with the five-stage contract: stages 1–2 are preparation/configuration; stage 3 proves real remote execution; stage 4 proves cache reuse or measured no-work; stage 5 verifies the artifact.
- [ ] Validate identity before restore and after restore, and preserve any pre-existing output.
- [ ] A separately approved, budget-aware build run is explicitly requested.

## Current outcome

This is a reviewable state-transfer contract, **not a runnable or build-ready five-stage pipeline**. No Chromium sync, GN generation, compilation, APK packaging, remote-execution test, checkpoint upload, or CI pipeline trigger was performed to create this document.
