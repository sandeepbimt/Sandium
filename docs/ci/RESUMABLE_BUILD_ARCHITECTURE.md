# Sandium resumable Android APK build architecture (v2)

**Branch:** `build/sandium-resumable-v2`

**Purpose:** produce the Brave-based Sandium Android arm64 Debug APK within CircleCI's available resources, while retaining valid partial compilation state when a stage cannot finish.

This is an implementation contract, not a claim that a production APK has already been built. No Chromium build is started by these changes.

## Core design

Keep three kinds of state separate because they have different compatibility rules:

1. **Chromium source/dependency cache** — expensive source checkout and gclient-managed dependencies. It must not contain Brave Core or `src/out`.
2. **Siso action cache** — content-addressed reusable build actions. It is shared across branches because action identity is determined by inputs; stale entries should be harmless cache misses, not trusted build outputs.
3. **Build-output checkpoint** — generated GN graph, output files, build state, and Siso filesystem/journal state. It is restored only through the checkpoint integrity and build-input/GN fingerprint checks.

A verified APK cache is separate again. It is accepted only after the artifact/state/manifest hashes and APK structure/signature evidence agree.

## Stable Chromium source cache

Before restoring Chromium, each job checks out Brave Core into `brave-core/` and snapshots the exact sync-identity inputs into `.sandium-source-cache-inputs/`. This keeps the files available for CircleCI checksum expansion even after the Brave checkout is moved under `src/brave` and temporarily excluded from the Chromium-only cache.

The source-cache key is independent of branch and CI-only commit SHA. It includes checksums for:

- manual Chromium pin;
- Brave `DEPS`, package metadata and pnpm lockfile;
- Brave sync configuration and sync implementation files.

The cache manifest additionally records the runtime fingerprint emitted by `ci/siso/source_cache_key.py`. Continuation stages recompute that fingerprint after checking out the same Brave revision and reject a mismatch before compiling.

Older branch-scoped v6 caches remain as fallback inputs. A v6 cache is not accepted as a completed build; Stage 1 must still validate the pinned source and run the sync path, then writes a v7 cache with the stable identity.

## Shared Siso action cache

The Siso cache uses a branch-independent v4 key family, with the previous v3 keys as fallback. This is the action cache, not the generated Android output directory. Build output still requires its own checkpoint validation.

The configured compile window requests `SANDIUM_CI_NINJA_JOBS=80` and `SISO_LIMITS=local=8,remote=80`, with strict remote execution enabled. The remote preflight intentionally uses a unique, uncached single C++ action and one remote job: it is a low-cost proof that remote execution works, not the main build's worker limit.

## Five-stage continuation

Every stage has a 3,000-second overall budget, reserves 600 seconds for handover, and caps its compile window at 2,400 seconds. Stages 1–4 defer a compile-command failure until after state recording, checkpoint packaging, cache-save steps, and artifact capture have had a chance to run. A genuine non-timeout compile failure still fails that stage after handover; it is not silently reported as a successful build. A bounded-window timeout (124) is accepted as an intermediate handover if checkpoint creation succeeds.

Stage 5 performs the final artifact checks and fails if no valid APK exists. The partial checkpoint is intended to remain available for a later run, subject to cache retention and compatibility validation.

A failure before compilation (for example, remote-preflight failure) cannot preserve compilation work that was never created. The source cache and diagnostics are the useful outputs in that case.

## Credit-protection rules

- Never trigger a production APK build merely to validate YAML, shell quoting, Python helpers, or workflow wiring.
- Run the inexpensive smoke workflow first; it now includes the stable source-cache identity tests and static resumable-pipeline contract tests.
- Do not start the APK build until the smoke checks pass and the current commit has been inspected.
- Treat remote-execution setup as unproven until the uncached preflight action is shown to execute remotely.
- Treat a cache as useful only after a later job/run reports a validated hit and its restored fingerprint/state matches. A save-cache step alone is not evidence of reuse.
- If a stage fails, inspect the exact failing step and preserved checkpoint metadata before changing build logic or launching another run.

## What must be demonstrated in CircleCI

The first low-cost validation should be the smoke workflow, not the production build. Once that passes, a production run is still required to establish:

1. the v7 source cache restores across branches when sync inputs are identical;
2. the Siso action cache restores useful entries;
3. the remote preflight passes and the main build reports actual remote actions/cache use;
4. each compile window saves a checkpoint that the next stage accepts;
5. a failed compile window preserves a compatible checkpoint for a later workflow;
6. Stage 5 produces and independently verifies the installable APK.

Until those are observed, this architecture remains source-level implemented but not end-to-end proven.
