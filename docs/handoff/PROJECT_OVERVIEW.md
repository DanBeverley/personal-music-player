# Project Overview

## Product

Neatie is an Android-first personal music application built with Flutter and a Python/FastAPI backend. It combines:

- personalized home recommendations;
- canonical music search;
- playable YouTube/YTMusic sources with optional Audius fallback;
- playlists, library, likes, history, and downloads;
- album, artist, mix, and radio details;
- lyrics and AI interpretation;
- song recognition;
- account-scoped persistence and optional cloud caches.

The product goal is a complete, responsive music experience with relevant discovery. A technically successful response that displays a thin or empty product is not acceptable.

## Repository Map

- `app/` — Flutter/Riverpod Android client.
- `ingestion/python_proxy/` — FastAPI server, recommendation worker, search, playback, catalog, assistant, recognition, and tests.
- `core_native/` — C++ audio/FFI source.
- `supabase/` — account/auth/database integration material.
- `runtime/` — ignored local SQLite state for recommendations and assistant memory.
- `tools/` — release/build helpers.
- `docs/handoff/` — authoritative continuation context.
- `resources/` — loose design/source resources; not all are runtime assets.

## External Responsibilities

| Service | Responsibility | Does it provide playable audio? |
| --- | --- | --- |
| MusicBrainz | Canonical artists, recordings, releases, release groups, credits, tags | No |
| Last.fm | Similar tracks/artists and tags | No |
| ListenBrainz | Popular recordings/releases and optional personal recommendations | No |
| YTMusic | Fast candidate enumeration and direct search | Identifies likely YouTube sources |
| YouTube Data API | Video/channel metadata and authority validation | No stream bytes |
| yt-dlp | Resolves actual YouTube stream access | Yes, when YouTube permits |
| Audius | High-confidence alternate source | Sometimes |
| Cloudflare R2 | Persisted stream and artist-artwork cache | Stores verified bytes |
| Supabase/Postgres | Authentication and optional synchronized user/recommendation data | No |
| Ollama/LangGraph | Ask Neatie and Behind the Lyrics | No |
| ACRCloud | Optional song recognition | No playback source |

## Core Design Decisions

- The backend is the only authority for active/prepared home-feed promotion.
- Home feed composition uses persisted candidate inventory; live broad retrieval does not belong in `/recommend`.
- Canonical identity and playable source identity are separate.
- MusicBrainz defines entities; it does not prove that an entity can play.
- Search selects one canonical lead and builds all artist-specific surfaces from it.
- Popular Radio owns an independent artist catalog and allocation space.
- History rows are hidden until earned rather than shortened or fabricated.
- `Frequently Listened` is play-count ordered; `Last Played` is chronological.
- Hidden Gems was removed because it duplicated Quiet Picks.
- Optional album/radio rows must be internally complete before appearing, but cannot prevent the core feed from being served.
- Provider failures are isolated and persisted; completed acquisition should not be discarded.
- Replaced systems are removed rather than retained as parallel safety layers.

## Current Branch and Checkpoints

- Active branch: `travel/work`
- Current committed head: `9fd0956`
- Backend checkpoint: `c378092`
- Flutter checkpoint: `30b0ae4`
- Branding checkpoint: `ac4ce49`
- Flutter dependency/launcher checkpoint: `9fd0956`

The `pubspec`/lock edits are already committed. The current working tree is a large intentional canonical search, artwork, progressive UI, and bounded source-resolution checkpoint. It should be split into logical commits; the handoff package and `AGENTS.md` are still untracked until committed. The current handoff supersedes the earlier claim that the final search/artwork correction was only awaiting device validation: multiple device passes have now exposed concrete remaining faults.

## Current Travel-Branch Outcome

What improved:

- Flutter client-side search caching and selection overrides were removed.
- Provider/MusicBrainz artist identities can coalesce through authoritative evidence.
- Search now fails closed on unresolved competing recording credits instead of knowingly expanding the wrong artist.
- Dio main and related artist artwork improved after R2 dependency/identity fixes.
- Related artists moved below target-specific results; artist albums precede regular albums.
- Background source verification is capped and systemic yt-dlp format failures open a short circuit.

What remains before search can be called complete:

- canonical recording evidence starts too late and can time out after classification;
- Flutter still promotes the first track to Top result when the backend returns no target;
- containing album resolution loses useful canonical release identity;
- album/playlist proxy paths are admitted before artwork bytes are verified;
- album detail artwork hydration does not update the canonical search catalog;
- snapshot revisions and logs advance without visible enrichment;
- latency and related-artist completion remain inconsistent.

The exact device-pass chronology and resolution matrix are in `CURRENT_STATUS.md`.

## What Was Proven Versus What Was Observed

Automated backend tests prove contracts against fixtures and controlled provider behavior. They do not prove:

- real YTMusic response time;
- YouTube bot/age/geo behavior;
- Flutter cache-file lifecycle;
- emulator queue audio continuity;
- real feed rotation timing;
- artwork persistence across device/backend restarts.

Always distinguish:

- **implemented** — code exists;
- **automated-test verified** — focused tests passed;
- **device verified** — the user observed correct behavior;
- **production proven** — repeated behavior with real providers.

The last inherited broad focused result was `127 passed` before later travel-branch changes. On 2026-08-02 the combined modified test gate could not start because the Codex shell had no installed/active Python interpreter. Do not describe this exact working tree as automated-test verified until the commands in `NEXT_STEPS.md` run successfully.

## Travel Continuation Model

The Codex conversation itself does not live inside this repository. A new agent should use:

1. `AGENTS.md`
2. `docs/handoff/CURRENT_STATUS.md`
3. `docs/handoff/NEXT_STEPS.md`
4. `docs/handoff/ARCHITECTURE.md`
5. `docs/handoff/KNOWN_ISSUES.md`
6. this file

The repository is already on the travel device and the active branch is `travel/work`. If another archive is made, it must still contain ignored private/runtime/native state as explained in `NEXT_STEPS.md`. Git alone does not carry `.env`, runtime databases, cookies, or packaged native `.so` files.
