# Sandium five-stage Android APK build contract

This document records the functional port of the successful SANDFOX reference run at
[`sandeepbimt/iceraven-browser@2b254b9`](https://github.com/sandeepbimt/iceraven-browser/commit/2b254b9)
to the Brave-based Sandium repository.

The reference run is a behavioral template, not a source tree to copy verbatim:
SANDFOX built Afterbird/Chromium 151, while Sandium builds Brave Core against the
Chromium tag pinned by `src/brave/ci/pins/chromium-tag.txt`.

## Opt-in and execution mode

- `run_sandium_android_apk_build` remains `false` by default, so an ordinary
  push does not start an Android APK build.
- When an APK build is explicitly requested, `run_sandium_compile_windows`
  defaults to `true`, selecting the complete five-stage build workflow.
- Setting `run_sandium_compile_windows=false` explicitly selects the limited
  preflight workflow; it must not be mistaken for an APK-producing build.

## Reference behavior being preserved

The SANDFOX configuration uses a five-stage, serial workflow. Stage 1 attempts
the build. Stages 2–5 first check whether a previous stage has already produced
a verified APK. A verified APK means the later stage should exit successfully
without repeating dependency setup or compilation. If no verified APK exists,
the stage restores the previous checkpoint, resumes the same build, records
progress, and saves a checkpoint for the next stage. The pipeline must fail if
the APK has not been produced after stage 5.

## Sandium implementation mapping

| SANDFOX reference responsibility | Sandium implementation |
| --- | --- |
| Pin and validate the source revision | Validate the Brave Core checkout, manual Chromium pin, resolved Chromium tag, and source-cache manifest. |
| Install build tooling and dependencies | Install pinned Node/pnpm, restore pnpm cache, install Android build dependencies on each fresh CircleCI machine, and run Brave's sync/hooks. |
| Configure Siso and BuildBuddy REAPI | `configure-sandium-remote-cache` writes the ignored Brave `.env`, configures the credential helper and local/remote caches, and validates the expected Siso settings. |
| Require the intended GN/build configuration | Validate generated GN arguments and require `use_siso=true`, `use_reclient=false`, and `use_remoteexec=true`; verify the Android arm64 Debug APK target. |
| Restore a compatible build checkpoint | `restore-sandium-build-output` restores the stage-specific checkpoint and `ci/siso/checkpoint.py restore` validates/extracts it; build-state checks reject incompatible output rather than silently cold-building in a continuation stage. |
| Run a bounded build slice | `ci/siso/run_compile_window.py` runs the Android APK compile with a stage budget, a handover reserve, and Siso metrics collection. The compile command uses the same target/configuration across stages. |
| Prove useful continuation | `ci/siso/build_state.py` records the build fingerprint/state; checkpoint and Siso statistics are saved for each stage. Remote-execution evidence and progress gates are used rather than treating a green command alone as proof of useful work. |
| Save state for the next stage/workflow | `ci/siso/checkpoint.py pack` and the `sandium-checkpoint-v7-...` cache keys preserve the compressed output checkpoint, metadata, and action frontier. The persistent Siso local cache is saved separately. |
| Verify and hand over a completed APK | `ci/siso/package_apk.py` writes the APK, manifest, state, signature/integrity evidence, and SHA-256 metadata. The reusable APK cache is accepted only when the source revision, target/config, fingerprint, file size/hash, ZIP contents, and signature-verification records agree. |
| Fast-exit after completion | Every stage restores and independently checks the revision-scoped verified APK cache before expensive source/dependency/build work; a verified hit halts the stage successfully. |
| Enforce the five-stage limit | Stage 5 independently verifies the final artifact and fails explicitly if no APK has been produced. |

## Stage responsibilities

### Stage 1 — source foundation and first compile window

1. Start the stage budget clock.
2. Restore and verify a reusable APK; if valid, exit without rebuilding.
3. Restore and validate the Chromium-only source cache.
4. Check out the exact Sandium/Brave Core revision and validate the manual Chromium pin.
5. Install pinned Node/pnpm and reuse the lockfile-matched dependency cache.
6. Run the pinned Brave sync for Android arm64, including Chromium/Brave dependencies and hooks; verify the Siso version and resolved Chromium tag.
7. Save the reusable Chromium-only source cache without Brave Core or build output, then restore those job-local directories.
8. Configure BuildBuddy/Siso, install Android build dependencies, run pre-compilation checks, prepare the source overlay/branding, and validate the GN executable.
9. Run bounded compile window 1 with remote execution enabled.
10. Validate the effective GN flags, record build state, package the APK if it exists, pack the cumulative checkpoint, save caches, and publish diagnostics/artifacts.

### Stage 2 — graph and compile continuation

1. Start the clock and check the verified APK cache; exit on a verified hit.
2. Restore validated source, Brave Core checkout, the Stage-1 output checkpoint, and the Siso local cache.
3. Reuse pinned Node dependencies; install fresh-machine Android build dependencies and configure BuildBuddy/Siso.
4. Rehydrate Brave Core dependencies and run hooks **without updating the pinned Chromium revision**; verify that the Chromium commit/tag and source manifest are unchanged.
5. Preflight GN, prepare the source overlay/branding, and validate effective remote-execution arguments.
6. Run bounded compile window 2; record Siso statistics and build state, package the APK if available, and save the cumulative checkpoint/cache/artifacts.

### Stage 3 — compile continuation

1. Start the clock and check the verified APK cache; exit on a verified hit.
2. Restore source, Stage-2 output checkpoint, and Siso cache; restore Node dependencies and install machine-local Android dependencies.
3. Preflight GN, prepare the source overlay/branding, and validate effective remote-execution arguments.
4. Run bounded compile window 3 against the same pinned source, output directory, target, and remote action cache.
5. Pack/save the cumulative checkpoint, preserve APK cache and diagnostics, and publish artifacts.

### Stage 4 — finish and package

1. Start the clock and check the verified APK cache; exit on a verified hit.
2. Restore source, Stage-3 output checkpoint, and Siso cache; restore Node dependencies and install machine-local Android dependencies.
3. Preflight GN, prepare the source overlay/branding, and validate effective remote-execution arguments.
4. Run bounded compile window 4 against the same pinned build.
5. Pack/save the cumulative checkpoint, preserve APK cache and diagnostics, and publish artifacts.

### Stage 5 — verify and publish

1. Start the clock and independently validate any reusable APK cache; exit on a verified hit.
2. If no APK is available, restore source, Stage-4 output checkpoint, and Siso cache; restore Node dependencies and install machine-local Android dependencies.
3. Preflight GN, prepare the source overlay/branding, and validate effective remote-execution arguments.
4. Run bounded compile window 5 against the same pinned build.
5. Record state and package the APK if it appeared; save the final checkpoint and artifacts.
6. Fail if the APK is still missing. Otherwise independently compare the artifact hash with state/manifest, validate ZIP integrity and Android manifest/DEX entries, and require signature-verification evidence.

## Important differences from SANDFOX

These are intentional platform-specific adaptations, not omissions:

- Chromium/Afterbird checkout and patch application are replaced by Sandium's Brave Core sync and manual Chromium pin policy.
- The build target/output is Sandium's Android arm64 Debug APK (`src/out/android_Debug_arm64` and `sandium-output/Brave-debug-arm64.apk`), not `ChromePublic.apk`.
- Sandium's verified APK cache is stronger than a simple completion marker: it checks the source revision, build fingerprint, artifact hash/size, ZIP integrity, package metadata, and signature-verification evidence.
- Each CircleCI job has a fresh machine. Machine-local Android dependencies and BuildBuddy/Siso configuration must therefore be set up on a continuation stage when the APK fast-exit does not apply.
- A checkpoint restore is not itself proof that work was reused. The build must preserve compatible Siso state/output and provide remote-execution and useful-progress evidence.

## Validation status

The configuration change enabling the complete compile workflow by default when the APK build is explicitly requested is committed separately. This document describes the source-level contract; it does not claim that a new Android APK build has run or passed.
