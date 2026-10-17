# SANDIUM Persistent Cache Implementation Plan

Status: review branch only. No CI run authorized by this file.

## Acceptance criteria

1. Expensive Chromium source/dependency data is reused across stages and separate CircleCI workflows.
2. Compilation action results are reusable across stages and workflows through the SANDIUM-only Siso/BuildBuddy remote cache.
3. Every resumed job verifies the Brave Core commit, Chromium revision, dependency state, GN arguments, Siso revision, and cache schema before building.
4. Partial or invalid source cache entries are never treated as a successful sync.
5. The final job succeeds only when the expected APK exists and integrity checks pass.
6. Diagnostics prove source-cache restore/save time, source-cache bytes, Siso remote execution, remote-cache reads/writes and cross-run cache hits.
7. No CI run is triggered during review.

## Design

### Layer A — CircleCI persistent source cache

- Cache the synchronized Chromium source/dependency tree under `src/`, excluding the fresh Brave Core checkout at `src/brave`.
- Cache only after `pnpm run init`, hooks, Chromium pin checks and a cache manifest all succeed.
- Before saving, move the fresh Brave Core checkout outside `src/`; save `src/` and non-secret sync metadata; restore the Brave checkout to its original path only after cache save completes.
- On each later job/run, checkout the pinned Brave Core source first, restore the most recent cache matching the source/cache schema, and then validate the cached Chromium pin and manifest.
- Use immutable, unique save keys per run and a prefix restore key. Never overwrite an immutable cache key.
- Include cache schema, target OS/architecture, Chromium pin, Brave package/lockfile pins and patch inputs in the fingerprint. Do not put credentials, `.env`, CI tokens, or BuildBuddy keys in the cache.
- Use `--no-history` for the initial synchronization only after confirming the option reaches gclient and that the resulting checkout is complete for this build.

### Layer B — BuildBuddy/Siso action cache

- Use a SANDIUM-only remote-execution instance and stable cache namespace.
- Keep the same source/toolchain/GN fingerprint across compile windows.
- Each compile window regenerates or validates the build graph and invokes the same pinned Android target; Siso/BuildBuddy should skip matching actions and execute only missing actions.
- Do not describe this as restoring a local object directory. Remote action-cache reuse and local build-graph progress are distinct.
- Capture Siso version, command line, remote action counts, cache hit/miss counts, transfer bytes and build target progress as artifacts.
- Do not assume the currently configured backend/auth check proves remote compilation works.

## Work distribution

1. Source foundation: restore any prior source cache, synchronize pinned source/dependencies, run hooks, verify pins and save a complete source cache.
2. Graph preparation and first compile window: restore source cache, validate fingerprint, configure GN/Siso and run a bounded compile window.
3. Compile continuation: restore source cache, validate the identical fingerprint, invoke the same target and rely on remote action-cache hits.
4. Compile/package continuation: repeat until the build command completes and the expected APK is present; an elapsed time window alone is not success.
5. Verify/publish: validate APK ZIP CRC, manifest, DEX, package metadata, signature and SHA-256; publish only the verified APK.

Each compile window must reserve time for logs and metrics. It must distinguish an intentional time-window stop from a real compiler failure. The final compile stage must fail if the target remains incomplete.

## Important cold-start limitation

A source cache cannot be saved until the first complete synchronization succeeds. The first cold sync is therefore still a gating risk. A partial Git checkout is not a valid checkpoint. The first run must either finish sync inside the allowed window or use a separately validated, persistent Git mirror/volume that survives interrupted jobs. Do not claim resumable first-sync behavior without evidence.

## Offline review checks

Before enabling the workflow:
- Parse CircleCI YAML and validate every job/requirement edge.
- Verify each cache path exists and excludes the fresh Brave checkout and secrets.
- Verify cache save is reachable only after successful source sync and pin validation.
- Verify every compile stage restores the source cache and uses identical target arguments/fingerprint.
- Verify bounded timeout status is handled only as an expected intermediate-stage stop; actual compiler failures fail immediately.
- Verify the final stage cannot publish unless compilation completed and APK verification passed.
- Confirm workflow trigger remains bound only to the approved build branch. Never trigger from this review branch.

## Go/no-go evidence required after approval

A later explicitly approved test must demonstrate:
1. Cold source cache save and measured archive size/time.
2. Warm restore from a separate workflow with matching source fingerprint.
3. Successful Siso remote action execution and cache writes.
4. Cache hits on a second invocation in the same workflow.
5. Cache hits in a separate workflow.
6. Build progress advancing across compile windows and eventual APK verification.
7. Measured CircleCI credits/storage and BuildBuddy transfer bytes.

Until all gates pass, this is an implementation design, not a proven resumable build.

## Review findings and remaining gates (2026-10-17)

- The pinned `build/commands/scripts/sync.ts` declares `--no-history` and forwards it to gclient as `--no-history`; this confirms option forwarding, not that every Android dependency/hook is complete or that cold sync fits the time budget.
- The review-only CircleCI candidate now rechecks the pinned Chromium version and emits a build fingerprint in each compile window. The source restore command also checks the manifest's Chromium and exact Brave Core commits.
- The source cache deliberately requires an exact Brave Core commit match. This is safe against silently using stale Brave patch/source state, but a new Brave Core commit will reject an older source cache even if the Chromium pin is unchanged. Cross-run reuse across Brave-only commits is therefore **not implemented** yet; do not loosen this guard without a separately designed and tested cache compatibility rule.
- Intermediate compile windows accept only timeout exit status 124 as an expected stop. Any other nonzero status fails the job. This reduces, but does not eliminate, the risk of confusing a timeout with compiler failure.
- The review candidate has not been parsed by a CircleCI YAML validator, run through shellcheck, executed on a runner, or tested against actual CircleCI cache restore/save or BuildBuddy/Siso metrics. Those remain hard gates. No claim of functional cache reuse is made.
- The candidate saves a full source tree, so archive size and save/restore duration may erase the expected benefit. Measure these before considering activation.

## Validation checkpoint (2026-10-17, offline)

**NO-GO: do not activate or run the review candidate yet.**

A source-level inspection found a definite Stage 1 configuration failure: `ci/siso/write_buildbuddy_env.py` reads `SANDIUM_REAPI_ADDRESS` and `SANDIUM_REAPI_INSTANCE` from the environment and rejects empty values, but the Stage 1 command invokes it without exporting either variable. Stage 1 will therefore fail before `pnpm run sync`. The candidate must set the reviewed values `remote.buildbuddy.io:443` and `sandium/android-arm64/ci` before invoking the helper.

Validation status:
- Verified by reading the pinned `sync.ts`: `--no-history` is a declared option and is forwarded to gclient.
- Verified by reading the helper: both SANDIUM variables are mandatory and validated.
- Static workflow inspection confirms five sequential job names and required dependency edges.
- Not verified: CircleCI's own config parser, shellcheck, actual cache save/restore, remote execution, remote-cache hit rates, or APK output. No CI execution was performed.

### Follow-up validation (2026-10-17)

The missing BuildBuddy environment variables have now been added to both call sites in the review-only configuration:
- Stage 1 exports `SANDIUM_REAPI_ADDRESS=remote.buildbuddy.io:443` and `SANDIUM_REAPI_INSTANCE=sandium/android-arm64/ci` before generating the runtime `.env`.
- The reusable remote-cache configuration command exports the same values before generating `.env`, covering compile stages 2–4.

A fresh static inspection confirms both helper call sites have both required values, the workflow contains five sequential jobs, and the active build branch config SHA remains unchanged at `be111053e53bdbe4cf0cca81c377b5b8ad401e72`.

**Still NO-GO for activation:** this environment does not have an available CircleCI config validator or shellcheck invocation wired into the offline review, so full YAML/schema and shell syntax validation is not certified. No CI run, cache test, remote execution test, or APK build has been performed. The next gate is a real parser/linter check of this review file only, without triggering a pipeline.

### Source-layout and fingerprint correction (2026-10-17)

The first five-stage draft incorrectly treated the CircleCI workspace root as the Chromium source root. In this checkout, Brave Core is checked out at `src/brave`, and its declared Chromium directory is `src/brave/src`. The review config has now been corrected to use that nested path consistently for Chromium version/commit checks, Android build-dependency installation, cache root, and APK discovery.

- Source cache schema/key bumped to v2 and scoped to the exact Brave Core revision, Chromium pin, and unique build number. The cache root is `src/brave/src`; the fresh Brave Core checkout itself is not moved or cached.
- The compile-stage fingerprint is now persisted from stage 2 through the CircleCI workspace. Stages 3 and 4 reconstruct the current input fingerprint and compare it to the stage-2 fingerprint instead of overwriting it with a new fingerprint.
- These changes are source-level corrections only. CircleCI YAML/ShellCheck validation is available via the manually dispatched GitHub Actions validator, but it has not yet been rerun against this latest commit. No CircleCI cache restore/save, BuildBuddy/Siso cache-hit behavior, remote execution, or APK build has been tested.
- The source cache remains a candidate design. It must not be activated until the updated config passes validation and its cache behavior is tested with explicit approval.
