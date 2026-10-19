# SANDIUM CI modes — keep Mode 2 available

This file is the durable operating contract for the five-stage Android pipeline.
**Mode 1 and Mode 2 are separate, opt-in CircleCI workflows. Neither is a replacement for the other.**

## Mode 1 — lightweight five-stage smoke test

Parameter: `run_sandium_ci_smoke_test=true`

- Runs five small CircleCI jobs in sequence on medium runners.
- Checks that Mode 2 remains configured, runs offline build-state tests, and verifies stage checkpoint handovers across independent jobs.
- Uses only small JSON checkpoint records in a dedicated workspace. It does not sync Chromium, invoke the Brave build command, or create a production APK.
- Smoke checkpoints are explicitly marked `mode=smoke` and `production_apk_created=false`.
- Its purpose is to debug CI syntax, job dependencies, workspace continuity, and test logic cheaply. A green smoke run is not evidence that Chromium compiles or Siso remote execution works.

## Mode 2 — real Brave/Chromium Android debug APK build

Parameter: `run_sandium_android_apk_build=true`

- Uses the existing `sandium-android-debug-apk` workflow and the five original jobs:
  1. `stage-1-source-foundation`
  2. `stage-2-graph-and-compile-window`
  3. `stage-3-compile-continuation`
  4. `stage-4-finish-and-package`
  5. `stage-5-verify-publish`
- Keeps the pinned Brave/Chromium source, real GN generation, Siso/remote execution settings, resumable build output, APK packaging, signature/integrity checks, and APK artifact handoff.
- It remains disabled by default and must only be run deliberately after the static validator and all five Mode 1 smoke jobs are green.

## Safe triggering

Set exactly one mode parameter to `true` for a pipeline run. For Mode 1, explicitly set `run_sandium_android_apk_build=false`. For Mode 2, explicitly set `run_sandium_ci_smoke_test=false`.

Do not share cache namespaces or workspace artifacts between modes. Never let a smoke fixture satisfy the production APK cache or publish checks.

## Promotion checklist: Mode 1 → Mode 2

- [ ] GitHub CircleCI schema and embedded shell/Python checks pass.
- [ ] All five smoke jobs pass in order.
- [ ] Stage 5 confirms the full five-checkpoint chain and identifies the run as smoke-only.
- [ ] Confirm no smoke cache/artifact is accepted by Mode 2's production APK validation.
- [ ] Trigger Mode 2 explicitly; verify real GN generation, Siso evidence, compilation, and independently verified APK.

**Do not delete or rename the Mode 2 parameter, workflow, jobs, source pin, compile windows, or APK verification path while working on Mode 1.**
