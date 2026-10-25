# SANDIUM Android Browser

**Only active browser source:** [Bare Browser](https://github.com/BareBrowser/bare-browser)  
**Source rules:** [SANDIUM_SOURCE_OF_TRUTH.md](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_SOURCE_OF_TRUTH.md)  
**Current status:** [SANDIUM_STATUS.md](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_STATUS.md)

## Current branch status

This branch is an isolated Bare migration workspace. Its active CircleCI configuration contains:
- An opt-in source-only preflight (default off).
- A full-build workflow that fails closed before source sync or compilation.

The five-stage compile/restore/publish workflow is **not yet wired**. The existing CircleCI account's ability to persist the Chromium source and build output within current limits must be proven before that workflow is implemented.

## Source boundary

Use only Bare Browser and the exact Chromium revision pinned by the selected Bare snapshot for current and future browser-source work. Do not use historical source-specific build scripts, cache identities, checkpoints or commands as implementation guidance. Read SANDIUM_SOURCE_OF_TRUTH.md before making changes.

## Preserve the existing integration

Reuse the existing Sandium GitHub repository, CircleCI project, BuildBuddy endpoint/instance, protected credential assignment, Siso integration and cache service. Do not recreate projects, rotate/reassign credentials, clear caches or trigger a browser build during migration.

## Adapter files

- ci/bare/source-pin.txt — source and target pins.
- ci/bare/gn_args.gn — pinned Android ARM64 Debug GN arguments.
- ci/bare/prepare_source.sh — pinned Bare patch and extension validation; optional patch application only to an exact clean Chromium pin.
- ci/bare/configure_siso.py — existing BuildBuddy REAPI settings in generated .gclient.
- ci/bare/write_cache_identity.py — pre-build source/config/toolchain identity.
- ci/bare/build_identity.py — source/build manifest and APK integrity/signature verification.
- ci/bare/checkpoint.py — identity-bound checkpoint pack/verify/restore.
- ci/bare/bootstrap_chromium.sh — explicitly gated Chromium bootstrap; never invoked by preflight.
- ci/bare/package_artifact.sh — Sandium APK naming, manifest and checksum.

No browser build is authorized by this README.