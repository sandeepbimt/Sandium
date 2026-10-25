# SANDIUM Android Browser

**Current browser source:** [Bare Browser](https://github.com/BareBrowser/bare-browser)  
**Authoritative project rules:** [SANDIUM_SOURCE_OF_TRUTH.md](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_SOURCE_OF_TRUTH.md)  
**Project status:** [SANDIUM_STATUS.md](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_STATUS.md)

## Important: source migration in progress

Sandium's only active browser-source authority is Bare Browser and the exact Chromium revision pinned by the selected Bare snapshot. Do not use this default branch as a build-ready browser source. The source migration is being developed on the branch named migration/bare-source-adapter.

That branch now contains a Bare-only, opt-in source preflight and a full-build workflow that fails closed. The five-stage compile/restore/publish pipeline is not yet wired because persistent Chromium source and build-output storage has not been proven within the existing CircleCI account. The existing CircleCI project, BuildBuddy endpoint/instance, protected credential assignment and cache service remain unchanged.

## Infrastructure preservation

The source migration must reuse the existing Sandium GitHub, CircleCI, BuildBuddy and Siso integration, including current protected secret assignments and cache configuration. Do not recreate projects, rotate/reassign tokens, clear caches or trigger a browser build during migration.

## Before any work

1. Read the [source-of-truth rule](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_SOURCE_OF_TRUTH.md).
2. Read the [Bare adapter plan](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_BARE_BUILD_ADAPTER.md).
3. Check the [current status and next action](https://github.com/sandeepbimt/Sandium-Docs/blob/main/SANDIUM_STATUS.md).

No browser build is authorized by this README.