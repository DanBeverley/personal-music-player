# Neatie Agent Guide

This repository is an active, cross-stack music application. Before changing code, read the handoff package in this order:

1. `docs/handoff/CURRENT_STATUS.md`
2. `docs/handoff/NEXT_STEPS.md`
3. `docs/handoff/ARCHITECTURE.md`
4. `docs/handoff/KNOWN_ISSUES.md`
5. `docs/handoff/PROJECT_OVERVIEW.md`

For hosting, backup or migration work, also read `docs/handoff/OPERATIONS.md`. Paths may contain credentials; document locations, never secret values.

## Working Rules

- This worktree uses `codex/web-app`. Main develop remains committed `e720709`; the web application checkpoint is `e4e7529`, including committed H01–H14, album transport and the quiet minimal frontend. Read CURRENT_STATUS for the verified serving build. The pre-redesign source tag is `neatie-web-before-editorial-20261005` at `f94103d`. Tests/runtime/private files remain excluded from commits. Inspect current Git status before editing or committing; never reset, clean or revert unrelated files.
- Inspect the implementation before adding a new subsystem. The project has previously accumulated overlapping recommendation paths and defensive fallbacks.
- Prefer root fixes over fallback-only patches. Do not make a weak feed look successful, and do not add another artifact quality state.
- Do not add hardcoded language, artist, title, or song blocklists as product logic. Home admission must use canonical identity, relationship evidence, source authority, and profile compatibility.
- The backend is the sole authority for active/ready home-feed promotion. Flutter may validate scope/version and provide offline recovery, but must not independently reject backend promotions.
- Customer-facing feed composition must consume persisted candidate inventory. Live MusicBrainz/YTMusic calls belong in bounded background enrichment, not the normal feed response path.
- Keep Popular Radio independent from regular row allocation. Artist catalog, taste mix, and discovery mix radios have different contracts.
- There is one canonical `/search` implementation. Fix that path from evidence; do not create another search engine or restore the removed broad-query path.
- Canonical identity is not proof of playability. Do not publish album or artist-work cards that cannot open to a usable persisted detail payload.
- Do not print or commit secrets from `.env`. Document environment variable names only.
- Use `apply_patch` for manual edits. Run focused backend tests after backend changes. The user runs `flutter analyze` locally because it is unreliable in the agent environment.
- For travel/device parity, preserve ignored `.env`, `runtime/` and yt-dlp cookies separately. Android release builds use CMake and `managedJniLibs`; legacy ignored `app/android/app/src/main/jniLibs/` may exist in archives, but copying it alone does not establish current native parity.

## Immediate Priority

Follow all 66 tracked entries and their Fix/Validate/Recheck/Decide/Deferred status in `docs/handoff/NEXT_STEPS.md`. H01–H14 and album transport are deployed; fresh supply/device acceptance remains open. The October 5 redesign follows the user's quiet minimal, desktop-first preference; retain its source and bundle rollback. Oracle is stopped and A1 is optional. September 20 runtime was restored on AWS; the newer downloaded AWS archive is hash/SQLite verified but not full-restore tested. Current priorities include fresh artwork/artist acceptance, playable album/radio supply and Home device acceptance. The last October 4 audit found three of 110 albums passing source coverage, zero persisted qualified inventory and nine reserve/eight visible radio cards failing depth/discovery; these are dated observations, not current counts. Retain Android account-ownership defects, playlist concurrency/two-device gates and pending iPhone acceptance. Keep playlist writes off until validated. Preserve accepted Android behavior; do not rebuild deployed fixes, restart Search/Home architecture or claim unverified device behavior fixed. Non-sensitive database audits are authorized; ask before sensitive access. Handoff is six owning documents; update them instead of adding a dated report per checkpoint. Documentation-only requests do not authorize application changes or deployment.
