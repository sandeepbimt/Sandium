# Sandium Chromium update policy

Sandium uses a **manual Chromium pin**. Brave Core's `package.json` is informative, but it must not silently advance Sandium's Chromium checkout.

## Current pin

The active source tag is recorded in `ci/pins/chromium-tag.txt`. Keep exactly one four-part numeric tag in that file.

## Normal build behavior

- `write_buildbuddy_env.py` places the approved tag into the Brave runtime config as `sandium_chromium_tag`.
- Brave's sync/build config resolves Chromium's version and source ref from that explicit value, not the package's current declared tag.
- When a restored Chromium checkout, source-cache manifest and `.gclient` content hash all match the pin, CI normalizes the old mtime-based sync record and reuses the checkout rather than launching a full Chromium sync.
- If the pin changes, the restored tag does not match, or `.gclient` content changed, the fast path declines reuse and Brave's normal sync path resolves the approved tag.
- CI records the actual Chromium tag/commit in the source manifest and build state. It never treats an unverified cache as a hit.

## Deliberate update procedure

Only update `ci/pins/chromium-tag.txt` when a Chromium refresh is explicitly approved for Sandium. Review Brave Core compatibility first; changing this one file triggers an intentional sync to the new approved tag and prevents reuse of the old Chromium source for that build. Do not change the pin just because Brave's package metadata changes.

The pin is a release-control mechanism, not a promise that arbitrary combinations of newer Brave code and older Chromium are compatible. The normal build/validation remains the compatibility check; if it fails, fix the compatibility issue or decide to update the pin rather than silently following upstream.
