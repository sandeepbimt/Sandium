# SANDIUM Android Browser

**Current browser source:** [Bare Browser](https://github.com/BareBrowser/bare-browser)  
**Authoritative project rules:** [SANDIUM_SOURCE_OF_TRUTH.md](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_SOURCE_OF_TRUTH.md)  
**Project status:** [SANDIUM_STATUS.md](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_STATUS.md)

## Important: source migration in progress

Sandium's only active browser-source authority is Bare Browser and the exact Chromium revision pinned by the selected Bare snapshot. This branch is a migration workspace, not a build-ready browser source.

The existing five-stage CircleCI / BuildBuddy / Siso integration is preserved, but inherited source-dependent steps are not yet compatible. All workflows are blocked by the parameter run_sandium_bare_source_ready=false until every source-dependent step, cache identity, checkpoint, artifact check and test is adapted and reviewed.

## Infrastructure preservation

The source migration must reuse the existing Sandium GitHub, CircleCI, BuildBuddy and Siso integration, including current protected secret assignments and cache configuration. Do not recreate projects, rotate/reassign tokens, clear caches or trigger a browser build during migration.

## Before any work

1. Read the [source-of-truth rule](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_SOURCE_OF_TRUTH.md).
2. Read the [Bare adapter plan](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_BARE_BUILD_ADAPTER.md).
3. Check the [current status and next action](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_STATUS.md).

No browser build is authorized by this README.