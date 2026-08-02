# Neatie Agent Guide

This repository is an active, cross-stack music application. Before changing code, read the handoff package in this order:

1. `docs/handoff/CURRENT_STATUS.md`
2. `docs/handoff/NEXT_STEPS.md`
3. `docs/handoff/ARCHITECTURE.md`
4. `docs/handoff/KNOWN_ISSUES.md`
5. `docs/handoff/PROJECT_OVERVIEW.md`

## Working Rules

- The active travel branch is `travel/work`, currently based on the same committed head as `develop`; the working tree contains extensive intentional user/agent changes. Never reset or revert unrelated files.
- Inspect the implementation before adding a new subsystem. The project has previously accumulated overlapping recommendation paths and defensive fallbacks.
- Prefer root fixes over fallback-only patches. Do not make a weak feed look successful, and do not add another artifact quality state.
- Do not add hardcoded language, artist, title, or song blocklists as product logic. Home admission must use canonical identity, relationship evidence, source authority, and profile compatibility.
- The backend is the sole authority for active/prepared home-feed promotion. Flutter may validate scope/version and provide offline recovery, but must not independently reject backend promotions.
- Customer-facing feed composition must consume persisted candidate inventory. Live MusicBrainz/YTMusic calls belong in bounded background enrichment, not the normal feed response path.
- Keep Popular Radio independent from regular row allocation. Artist catalog, taste mix, and discovery mix radios have different contracts.
- There is one canonical `/search` implementation. Fix that path from evidence; do not create another search engine or restore the removed broad-query path.
- Canonical identity is not proof of playability. Do not publish album or artist-work cards that cannot open to a usable persisted detail payload.
- Do not print or commit secrets from `.env`. Document environment variable names only.
- Use `apply_patch` for manual edits. Run focused backend tests after backend changes. The user runs `flutter analyze` locally because it is unreliable in the agent environment.
- For travel/device parity, remember that `.env`, `runtime/`, yt-dlp cookies, and `app/android/app/src/main/jniLibs/` are ignored by Git and may exist only in the transferred archive.

## Immediate Priority

Finish the evidence-backed canonical search/artwork correction documented in `docs/handoff/NEXT_STEPS.md`, then device-validate it before changing feed architecture. The required correction is to remove the Flutter-invented top-result fallback, prevent canonical-evidence starvation, bind accepted tracks to authoritative artist/release identities, replace optimistic entity-artwork admission with persisted verification, and advance progressive revisions only for visible changes. If that gate passes, replace the single prepared-feed slot with the agreed two-feed ready queue and remove forced return-from-search refresh.
