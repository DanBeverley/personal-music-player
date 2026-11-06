# Project Overview

Neatie is a cross-stack music application with an Android Flutter client and a separate browser client for desktop and phones. Shared backend services supply Search, playable music sources, recommendations, metadata and lyrics conversation. Client UI/audio implementations differ; changes do not automatically cross worktrees.

## Product

- **Discovery:** canonical Search with songs/artists/albums, artist profiles and playable album details; persisted personalized Home and independent radios; Charts and New Releases.
- **Listening:** source-aware playback, related-song queues, lyrics and grounded AI conversation. Web uses a vinyl listening panel with cover/list queue views and song search; Android also supports downloads and native player integration.
- **Library:** accounts, likes and cloud playlists. The November 2 web release exposes named creation, song-to-playlist selection and stored creation dates through the existing guarded cloud editing functions. Guest web Home offers sign-in/Search without building personalized recommendations.
- **Operations:** bounded diagnostics, background preparation, cache retention/idle policies and protected rollback. Playback reliability, unfamiliar-artist discovery and physical iPhone performance still require acceptance; see NEXT_STEPS for the full queue.

The November 3–4 resource release removes duplicate profile/model ownership and repeated inventory bodies, adds lazy collection rendering and excludes Android-only branding from the web bundle. Local backups and retention now pass; verified compaction reduced the recommendation database from 28.55 to 5.04 GB. This reduces storage/cache work; it does not change the fixed EC2 hourly price. Hosting migration, sustained worker-memory acceptance and off-host/full-system recovery remain separate tasks.

Serving build/test/benchmark details belong in CURRENT_STATUS, not duplicated here. Historical proofs and recovery instructions remain in KNOWN_ISSUES and OPERATIONS.

## Six-document handoff map and read order

| Order / file | Owns |
| --- | --- |
| 1. [CURRENT_STATUS.md](CURRENT_STATUS.md) | Serving version, shipped changes, recorded tests and user acceptance |
| 2. [NEXT_STEPS.md](NEXT_STEPS.md) | Complete task queue, stable IDs, status and completion checks |
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

The main `develop` checkout preserves separate Android playlist delta-sync changes. The `codex/web-app` worktree owns current browser/backend releases. Do not build an Android release from its older native playlist copy or overwrite main's newer changes. Runtime, credentials and cookies are private state outside source control.

## External services and rules

Supabase owns auth/cloud library. R2 optionally stores durable validated stream/artwork bytes. YTMusic/YouTube and eligible Audius sources provide playable candidates; yt-dlp/browser transport acquire bytes. MusicBrainz provides canonical evidence, Last.fm/ListenBrainz discovery/charts, LRCLIB lyric fallback. Provider metadata or identity does not prove playback, meaningful art or usable details.

Preserve one canonical Search/source pipeline and one backend-owned Home lifecycle. Feed composition consumes persisted supply; provider work is bounded/background, and radio is independent. Canonical identity is not playability. Never expose credentials/private runtime in docs/Git. Documentation-only work does not authorize application changes/deployment.
