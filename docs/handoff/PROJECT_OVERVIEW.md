# Project Overview

Reconciled October 4, 2026. Neatie has an Android Flutter client and a separate Flutter web client for desktop/iPhone browsers, sharing canonical backend services and cloud accounts. They do not share identical UI/audio implementations. A change in one worktree is not automatically in the other or deployed.

## Product

Android exposes Search, durable Home/Popular Radio, source-aware playback/discovery queues, library/downloads, Charts/New Releases, lyrics/Behind the Lyrics, assistant/recognition and signed-update infrastructure. User accepted related-song queues, Search scrolling and New Releases baselines.

Web exposes responsive navigation, browser audio/local scoped queue persistence, auth/cloud likes/existing playlists, richer canonical Search, artist/album details and Home. It is deployed on AWS; full Android presentation, additional product surfaces, cross-device handoff and PWA/offline remain open. Browser playback is observed; long-session/background reliability is not fully accepted. Current status and all 66 task IDs live in the first two guides below.

## Six-document handoff map and read order

| Order / file | Owns |
| --- | --- |
| 1. [CURRENT_STATUS.md](CURRENT_STATUS.md) | Serving version, shipped changes, recorded tests and user acceptance |
| 2. [NEXT_STEPS.md](NEXT_STEPS.md) | Complete 66-entry queue, stable IDs, status and completion checks |
| 3. [ARCHITECTURE.md](ARCHITECTURE.md) | Module ownership, data flow and contracts |
| 4. [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | Current artwork/UI and account defects, historical causes, diagnostics and cleanup findings |
| 5. PROJECT_OVERVIEW.md | Product/worktree orientation and this map |
| Operations: [OPERATIONS.md](OPERATIONS.md) | Hosting, private file locations, backups, rollback and dated alternative-host research |

Dated release/investigation reports were merged into their owning guides rather than left as competing current-state documents. Keep dates, exact verification limits and unique recovery details when updating them; do not start a new report file for each minor checkpoint. Update the owning guide and queue status.

## Repository/worktree map

| Location | Responsibility |
| --- | --- |
| Main develop tree | Android bootstrap/logic plus unreleased playlist delta-sync changes |
| Web codex/web-app tree | Conditional Android/browser bootstrap and deployed web/backend work |
| app/lib/main.dart / main_android.dart | Platform entry; main_android exists in web tree |
| app/lib/web/ | Browser shell, audio/player, auth, library, catalog and diagnostics |
| app/lib/logic/ | Android state and reusable metadata |
| ingestion/python_proxy/auralis_backend/ | FastAPI, worker, Search/Home, source/stream, Charts/Releases, lyrics/assistant/recognition |
| ingestion/browser_audio/ | Optional browser-backed transport in web tree |
| deploy/production/ / tools/ | Compose/gateways/backup/deploy helpers and local checks |
| runtime/, .env, cookies | Ignored local/private state; Git alone is not a full runtime transfer |
| Android CMake / managedJniLibs | Current release native build; legacy ignored jniLibs may remain in archives |

The trees intentionally differ: main develop remains committed e720709; the web application checkpoint is 7553f5b, including H01–H14 and the October 5 quiet minimal frontend/refinement. H09 extends verification of existing history; H10–H14 improve presentation/freshness. The user's visual preference is quiet minimal with desktop priority: restrained dark surfaces, warm typography, soft sage accents, generous spacing, artwork and a compact playback dock. Home is the initial web destination; the phone dock expands into the same playback session. Refinement adds gentle motion, continuous forward album looping, paging arrows, Search spacing, equalizer loading and the existing brand mark as browser icons. The user-created neatie.duckdns.org hostname preserves the prior address; origin-local guest/auth state does not migrate automatically. CURRENT_STATUS owns the verified serving build. Fresh supply and device acceptance remain open. Inspect Git status and owning implementation; preserve unrelated changes.

## External services and rules

Supabase owns auth/cloud library. R2 optionally stores durable validated stream/artwork bytes. YTMusic/YouTube and eligible Audius sources provide playable candidates; yt-dlp/browser transport acquire bytes. MusicBrainz provides canonical evidence, Last.fm/ListenBrainz discovery/charts, LRCLIB lyric fallback. Provider metadata or identity does not prove playback, meaningful art or usable details.

Preserve one canonical Search/source pipeline and one backend-owned Home lifecycle. Feed composition consumes persisted supply; provider work is bounded/background, and radio is independent. Canonical identity is not playability. Never expose credentials/private runtime in docs/Git. Documentation-only work does not authorize application changes/deployment.
