# Known Issues and Guardrails

Last reconciled: 2026-08-12

## Search

Search is device-accepted and no longer an active redesign target. Remaining latency on unseen queries is primarily external provider and canonical-evidence time. Do not restore broad-query search, add another `/search` path, infer top results in Flutter, or trade canonical correctness for a faster guess.

Persisted search snapshots are shared canonical bases. Personal recent tracks and other weak user signals must remain overlays and must never contaminate the shared snapshot. Progressive revisions advance only for visible semantic changes and must merge monotonically.

## Feed Lifecycle

The backend still has a single prepared-feed slot. Return-from-search refresh and pull-to-refresh can therefore expose inconsistent timing when no ready replacement exists. The approved replacement is a backend-owned two-feed ready queue; Flutter must not become a second promotion authority.

## Playback

- First uncached playback may exceed one second.
- Earlier device audits showed later queue items advancing without audio.
- Canonical identity is not proof of a usable media source.
- YouTube sources can be bot-blocked, geo-blocked, private, deleted, or format-incompatible.
- Background verification must remain bounded and independent from user-requested playback.
- Machine-local native libraries may not match the Dart FFI contract.

## Artwork and Metadata

- Search artwork is substantially corrected through persisted verification and background repair.
- Feed/Popular Radio artwork still requires its dedicated lifecycle pass.
- Track/history thumbnails must be derived only from verified source evidence.
- Release-year coverage is incomplete; upload year is not release year.
- Never publish an album or artist-work card that cannot open to a usable persisted detail payload.

## Popular Radio

Popular Radio needs canonical seed artwork, independent rotation, preservation of the last complete card, and stronger duplication checks. It must remain independent from regular row allocation.

## Product Work Not Yet Completed

- complete artist discographies and authoritative statistics;
- Behind the Lyrics reliability and interpretation quality;
- live Charts/Trending graphs and statistics;
- continued release metadata coverage;
- structural decomposition of oversized modules after behavior is stable.

## Portable State

The following are intentionally ignored and must be transferred/configured separately:

- `.env`;
- `runtime/` SQLite state;
- yt-dlp cookies;
- `app/android/app/src/main/jniLibs/`;
- local Python/Flutter environments and caches.

Never commit secrets. Document environment variable names only.

## Repository Guardrails

- One discovery/recommendation engine.
- One canonical `/search` implementation.
- One playback/source-resolution path.
- Backend owns feed promotion.
- Customer-facing feed composition consumes persisted candidate inventory.
- Live provider calls belong in bounded enrichment, not normal feed response paths.
- Prefer root fixes over fallback-only patches.
- Do not add artist/title/language blocklists as product logic.
- Run focused tests for the touched contract; the user runs `flutter analyze` locally.
