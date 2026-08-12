# Current Status

Last reconciled: 2026-08-12

## Repository State

- `travel/work` is the merge candidate for `develop`.
- `develop` has no commits absent from `travel/work`, so the intended merge is fast-forward only.
- The abandoned `codex/search-latency-goal` worktree and branch were removed.
- Local Codex checkpoints, latency logs, and pytest temporary directories were removed and ignored.
- Secrets and machine-local runtime state remain ignored: `.env`, `runtime/`, yt-dlp cookies, and Android native libraries.

## Search Gate

Canonical search is device-accepted as functional and ready to leave the active product queue.

The current implementation has one backend `/search` authority and provides:

- classify-then-resolve canonical target selection;
- canonical artist and containing-album binding;
- persisted cross-account base snapshots with bounded LRU retention;
- semantic, monotonic progressive revisions;
- verified persisted artist/entity artwork and background repair;
- shared canonical suggestions with per-user recent-track overlays;
- typo, alias, partial-title, and reordered-token support;
- bounded initial responses with optional artist/catalog completion after publication;
- revision-wait polling without full repeated searches;
- stable scrolling and animated appended search cards.

Repeated device audits covered artist, track, typo, ambiguous-title, and unseen queries. The final user audit accepted search quality and behavior. Search latency is improved through persisted snapshot fast paths, bounded canonical evidence waits, deferred optional surfaces, and local-only first-response artwork checks. Unseen searches still depend on external provider latency, so performance should be monitored rather than redesigned again without new evidence.

## Verification

Latest focused pre-merge verification:

- backend Python compilation: passed;
- snapshot persistence/revision tests: 10 passed;
- retrieval barrier, snapshot scheduling, and first-response artwork tests: 7 passed;
- suggestion behavior tests: 5 passed;
- search artwork marker tests: 6 passed;
- duplicate-search suggestion contract: 1 passed;
- `git diff --check`: passed;
- conflict, binary-diff, and secret-assignment audit: clean.

The user performs `flutter analyze` and final Android device checks locally.

## Stable Product Baseline

- One production recommendation/discovery engine emits the ten supported home rows.
- Feed artifacts and candidate inventory are persisted.
- Popular Radio remains independently allocated from regular feed rows.
- Playback uses canonical metadata plus a separately verified playable source.
- R2-backed stream and artwork caches are available when configured.
- Search no longer owns or triggers home-feed promotion.

## Immediate Priority

Replace the single prepared-feed slot with the agreed two-feed ready queue and remove forced refresh on return from Search. After that, prioritize playback continuity/start latency and Popular Radio correctness. Do not reopen search architecture unless a reproducible regression violates the accepted canonical contracts.
