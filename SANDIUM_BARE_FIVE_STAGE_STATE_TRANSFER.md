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

## Reference implementation and reported results

Reference: [sandeepbimt/iceraven-browser, branch `sandfox/checkpoint-stage-v1`, commit `2b254b9`](https://github.com/sandeepbimt/iceraven-browser/tree/sandfox/checkpoint-stage-v1), especially `.circleci/config.yml`.

The reference configuration has a concrete staged-checkpoint pattern:
- stage-scoped CircleCI `save_cache`/`restore_cache` keys;
- a checkpoint archive plus metadata, including Siso state/journal and compiled output;
- an independent APK completion marker;
- early fast-exit in later stages once a previous stage recorded a ZIP-verified APK;
- a progress gate that rejects an expensive repeated stage if no useful progress or remote execution is demonstrated.

Prior project conversation records report that the `2b254b9` run produced a working APK, with Stage 1 taking about 27 minutes and Stages 2–5 taking roughly one second each after the APK-completion check. Earlier notes also report that Stage 2 restored a compatible output checkpoint associated with CircleCI job 140 and reused a 44.5 GB Chromium source cache and 735 MB Siso cache associated with job 146.

**Evidence qualification:** these timings, cache sizes and job references are carried forward from earlier conversation notes, not independently re-fetched from CircleCI in this turn. The supplied screenshot shows a run marked **Canceled** and does not itself prove end-to-end success. Do not treat the reported values as independently verified live logs. The implementation pattern is grounded in the checked-in config; the run outcomes still need direct log/artifact confirmation.

## Five-stage contract

| Stage | Responsibility | Required hand-off/evidence |
|---|---|---|
| 1 — source-foundation | Validate Bare pin, patch-series digest, uBlock CRX checksum, Chromium base pin and patch application plan | Immutable build identity record; no claim that a checkout exists unless verified |
| 2 — toolchain-and-graph | Verify depot_tools/Siso identity, effective GN args and generated build graph | Same build identity hash; GN args and toolchain evidence |
| 3 — compile-and-ledger | Perform incremental compilation with `use_remoteexec=true`, `use_siso=true`, `use_reclient=false` | Siso metrics/logs proving at least one actual remote-executed action; stage ledger |
| 4 — restore-and-continue | Restore only a verified matching checkpoint, then continue incremental work if needed | Same build identity; previous ledger; cache hit evidence OR explicit, measured no-work result |
| 5 — finalize-and-publish | Complete remaining build work, verify APK structure/signature, emit manifest and SHA-256 | Same identity; signed APK verification; manifest; checksum |

A stage may report “no work” only when evidence shows there are no remaining actions for the same build identity. An empty or missing metrics file is not proof of no work. Remote execution must be proven from Siso metrics/logs; GN flags alone do not prove that actions ran remotely. Cache reuse must be proven from metrics/logs; the existence of a cache configuration alone is not proof of a cache hit.

## Adaptation plan

1. **Separate source cache from output checkpoint.** Use distinct, immutable cache keys for pinned source/dependencies and for each stage's output checkpoint. Include branch/workflow/stage identifiers and source/build identity in the metadata. A cache key itself cannot be mutated after saving.
2. **Restore only the expected predecessor.** Stage N restores Stage N-1's checkpoint key for the current workflow. Cross-workflow reuse may be a fallback only when metadata matches the exact pinned Bare commit, Chromium base, ordered patch-series digest, CRX checksum, toolchain and GN identity.
3. **Package build graph, output and Siso state together.** Preserve `build.ninja`, `args.gn`, generated graph/state, compiled outputs, and `.siso_fs_state` and/or a valid journal recovery state. Do not assume Siso's remote action cache replaces this local state.
4. **Verify restore before compiling.** Check archive checksum, metadata schema, source/build identity and expected paths. Record restored object/output counts and Siso state mode. On a mismatch or missing checkpoint, fail closed; do not silently start an expensive clean sync/build.
5. **Gate every timed stage on useful progress.** Record before/after object and output counts, completed action/frontier counts, Siso remote execution evidence, cache metrics, build exit code, and checkpoint save result. If the stage times out without producing an APK, only continue if checkpoint validation and measurable progress pass.
6. **Make APK completion independently verifiable.** Only create `BUILD_FINISHED=true` after the APK is non-empty and ZIP-validated, and record SHA-256 and size. Later stages should restore/read this small marker first, verify it is consistent with artifact evidence, and halt immediately when valid.
7. **Keep the fast path separate.** Name source-only checks clearly (e.g. `FAST-Preflight`) and the full build `LONG-Full-APK`; the former must never sync Chromium or compile.

## State that must survive stage boundaries

1. **Source state:** exact Chromium base plus the complete ordered Bare patch series, Bare checkout and pinned uBlock CRX.
2. **Build state:** generated GN files, toolchain/dependency state and the complete `src/out/android_Debug_arm64` output tree required for incremental continuation.
3. **Siso state:** `.siso_fs_state` and/or a valid recoverable journal, preserved with graph/output files and tested after extraction.
4. **Evidence state:** source/build identity, Siso logs/metrics, stage ledger, checkpoint metadata and hashes.
5. **Artifact state:** final APK and manifest only after verification.

The metadata ledger is small, but it does **not** preserve the Chromium checkout or build output. The existing `ci/bare/checkpoint.py` can package/restore the output directory locally with identity checks; it does not by itself implement CircleCI cache save/restore or prove that Siso state survives a real stage transition.

## Current limitations and fail-closed decision

The Sandfox reference demonstrates a plausible mechanism, but adapting it safely requires:
- direct review of the cited successful CircleCI run's stage logs and artifacts;
- confirmation of the current CircleCI plan's applicable cache limits/retention;
- validation that the Bare output checkpoint is within those limits and that archive restoration preserves required Siso state;
- mapping the existing Sandium ledger semantics correctly to the five stages;
- preserving the current full-build parameters as false until all of the above is reviewed.

A cache restore miss must not silently trigger an expensive clean sync/build. CircleCI caches are immutable; keys must be unique and correctly scoped. Siso/BuildBuddy action cache is not a substitute for the source checkout, generated GN state, or all local build outputs.

## Non-negotiable safety gates

- Keep `run_sandium_bare_source_preflight=false`, `run_sandium_bare_source_ready=false`, and `run_sandium_bare_android_apk_build=false` by default.
- Never delete an existing cache, output tree, or checkpoint to force a cold run.
- Never overwrite an existing restore backup; stop and require review if one already exists.
- Reject any checkpoint whose source, patch, extension, toolchain, GN or build identity differs.
- Fail before Chromium sync/compilation when the persistence mechanism or required credentials are absent.
- Do not claim remote execution, cache reuse, or APK success without captured evidence.

## Readiness checklist

- [ ] Independently inspect successful reference run logs/artifacts and confirm stage durations, checkpoint sizes, restore result, Siso evidence, APK checksum/validation.
- [ ] Confirm current CircleCI plan cache limits and retention behavior.
- [ ] Test archive round-trip of Siso state/journal and file metadata on a harmless fixture.
- [ ] Adapt stage-specific immutable cache keys and identity validation for the Bare pins.
- [ ] Align stage-ledger semantics: stages 1–2 prepare/configure; stage 3 proves actual remote execution; stage 4 proves cache reuse or measured no-work; stage 5 verifies APK.
- [ ] Keep the full build disabled until separately approved for a budget-aware run.

## Current outcome

This is a reviewable state-transfer contract, **not a runnable or build-ready five-stage pipeline**. No Chromium sync, GN generation, compilation, APK packaging, remote-execution test, checkpoint upload, or CI pipeline trigger was performed to create this document.
