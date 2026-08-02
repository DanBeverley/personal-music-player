# Architecture

## System Shape

```text
Flutter client
    |
    | HTTP on port 8000
    v
FastAPI / Python proxy
    |
    +-- canonical search and catalog
    +-- candidate acquisition and feed state
    +-- playback/source registry and stream cache
    +-- details, lyrics, assistant, recognition
    |
    +-- SQLite runtime state
    +-- optional Supabase/Postgres sync
    +-- optional Cloudflare R2 objects
    +-- MusicBrainz / Last.fm / ListenBrainz / YTMusic / YouTube
```

`start_dev.ps1` loads the root `.env`, stops stale repo-owned Python processes, starts the recommendation worker when enabled, starts FastAPI on port 8000, then runs Flutter against `emulator-5554`.

For the Android emulator, the client reaches the host backend at `http://10.0.2.2:8000`. `127.0.0.1` inside Android is the emulator itself, not the Windows host.

## Flutter Client

### Entry and navigation

- `app/lib/main.dart`: application bootstrap and shared part structure.
- `app/lib/main_shell.dart`: authenticated shell and main navigation.
- `app/lib/main_home.dart`: home/search orchestration, refresh gestures, row paging, and search navigation.
- `app/lib/main_details.dart`: detail-screen routing.
- `app/lib/main_player.dart`: full-player composition.

### Core state

- `logic/audio_provider.dart`: central audio state and shared exports.
- `logic/audio_provider_recommendation.dart`: launch feed, `/recommend`, active/prepared versions, polling, refresh, and row pages.
- `logic/audio_provider_request_builder.dart`: recommendation request signals.
- `logic/recommendation_feed_models.dart`: row/feed models and backend diagnostics.
- `logic/home_feed_cache.dart`: offline/crash recovery for valid backend-active feeds.
- `logic/search_provider.dart`: query submission, search snapshots, paging, and progressive completion.
- `logic/search_payload_runtime.dart`: conversion from backend search payloads.
- `logic/details_provider.dart`: album/artist/track detail retrieval.
- `logic/audio_stream_runtime.dart`: playback resolution, prepared prefixes, cache source, and timing.
- `logic/audio_queue_runtime.dart`: queue progression.
- `logic/history_manager.dart`: persisted local history and backend history synchronization.
- `logic/track_metadata.dart`: canonical metadata and display helpers.
- `logic/recommendation_preferences_provider.dart`: Neatie/Blended/ListenBrainz-first settings.
- `logic/collection_likes_provider.dart`: likes for albums, mixes, and radio cards.

### Shared presentation

- `widgets/app_artwork.dart`: shared track/album/artist artwork behavior.
- `widgets/home/neatie_home_sections.dart`: home shelves, mixes, radio, albums, artists, and Quiet Picks.
- `widgets/home/search_surface_widgets.dart`: search input, tabs, results, paging, and loading visuals.
- `widgets/details/details_sections.dart`: artist, album, mix, and collection details.
- `widgets/player/*`: player, queue, lyrics, and Behind the Lyrics.
- `screens/radio_detail_screen.dart`: Popular Radio queue/detail screen.

Flutter must not reimplement feed quality. It may validate account scope/version and retain the last valid offline feed, but active/prepared promotion belongs to the backend.

## Recommendation Backend

### Request-independent candidate supply

```text
history/library/search/interaction signals
    -> canonical taste anchors
    -> acquisition ledger jobs
    -> structured provider results
    -> canonicalization and deduplication
    -> playable-source verification
    -> admitted CandidateInventory
```

Key modules:

- `discovery/enrichment.py`: bounded acquisition plans and persisted provider work.
- `discovery/structured_providers.py`: Last.fm, ListenBrainz, MusicBrainz-facing structured operations.
- `discovery/source_registry.py`: canonical recording to verified playback source mapping.
- `discovery/admission.py`: source authority, relationship, language/region, negative feedback, and profile compatibility.
- `discovery/inventory.py`: persisted canonical pools, coverage, reserves, ledger, and generation publication.
- `discovery/radio_inventory.py`: independent per-artist Popular Radio catalogs.
- `discovery/candidates.py`: converts persisted supply into candidate objects.

Customer `/recommend` responses should not perform broad external candidate discovery. Provider work belongs to background acquisition and is persisted incrementally.

### Feed construction

```text
ready CandidateInventory
    -> row builders
    -> reserved/global allocation
    -> post-allocation contracts
    -> DiscoveryArtifact
    -> active/prepared FeedState
```

Key modules:

- `discovery/ranking.py`: row-specific ranking, Made For You, albums, artists, and radio.
- `discovery/allocation.py`: cross-row canonical deduplication and row reservations.
- `discovery/artifact.py`: final contracts, serialization, row diagnostics, and shortage domains.
- `discovery/feed_state.py`: active/prepared state, versions, promotion, and dirty reasons.
- `discovery/service.py`: `/recommend` lifecycle and background preparation orchestration.
- `recommend/taste_runtime.py`: authoritative user taste and history evidence.
- `recommend/maintenance_runtime.py`: events, search learning, dirty signals, and maintenance.
- `recommend/feature_store.py`: persisted feature/feed records.

### Row contracts

| Row | Minimum | Target / maximum | Notes |
| --- | ---: | ---: | --- |
| Today’s Pick | 6 | 6 / 8 | Core discovery tracks |
| Featured Albums | 8 | 8 / 10 | Optional; only valid albums |
| Last Played | 8 when earned | 8 / 16 | Chronological history |
| Frequently Listened | 8 when earned | 8 / 16 | Canonical play-count ranking |
| Made For You | 5 | 8 / 12 | Each mix owns its queue |
| Because You Played | 12 | 12 / 48 | Pageable, anchor-related |
| Popular Radio | 8 cards | 12 / 12 | Optional; independent inventory |
| Recommended Albums | 12 | 12 / 48 | Optional; canonical playable albums |
| Recommended Artists | 10 | 10 / 48 | Playable catalog backing |
| Quiet Picks | 20 | 20 / 200 | Taste-connected retained reserve |

Popular Radio cards require twelve playable tracks to publish and continue enriching toward twenty-four.

### Feed lifecycle

- `active_feed` is the complete feed currently served.
- `prepared_feed` is a complete replacement waiting for promotion.
- Launch serves active immediately.
- Background work never mutates active.
- Pull refresh should promote an existing prepared feed first.
- A failed or incomplete replacement must leave active untouched.
- The server assigns monotonically increasing `feed_version` values.
- Flutter must not turn `preparing` into an empty feed when a valid active feed exists.

The long-term product decision still pending is to maintain two ready rotations rather than one prepared slot. That work must replace the current single-slot behavior instead of layering another client-side fallback over it.

## Search Architecture

The current production search path is one canonical `/search` service in `auralis_backend/search/service.py`. Flutter no longer owns a separate payload cache or selection-override engine; identical in-flight requests may be coalesced, but persisted search state belongs to the backend.

```text
normalized query
    -> direct/local retrieval
    -> unconditioned YTMusic/provider candidates
    -> provider-only entity-type classification
    -> type-gated canonical identity evidence
    -> accepted target bundle
    -> canonical artist catalog
    -> tracks/albums/artist works
    -> Last.fm related artists
    -> playlists
    -> artwork and playable/detail validation
    -> persisted search snapshot
    -> paged/progressive Flutter surfaces
```

Supporting modules:

- `search/canonical.py`: canonical identity helpers.
- `search/intelligence.py`: query normalization, aliases, typo/partial scoring, and intent evidence.
- `search/catalog_pipeline.py`: artist catalog and album/track construction.
- `search/musicbrainz.py`: MusicBrainz lookup helpers.
- `search/upstream_runtime.py`: bounded provider calls.
- `storage/artist_artwork.py`: canonical artist artwork objects, current album/playlist proxy transport, and R2 persistence.

Important rules:

- A submitted query is not attached blindly to every returned item.
- Entity type is classified before identity resolution. A MusicBrainz recording lookup may run concurrently for latency, but cannot affect the type decision and can only be consumed after `track` is locked.
- One canonical target bundle must be accepted before artist catalog construction. It owns the selected item, credited/lead artist, containing album, stable target identity, confidence, margin, and evidence.
- Artist works and albums must follow the chosen artist identity, not a provider text label alone.
- Provider intent is ranking evidence, not permission to append unrelated tracks indefinitely.
- Canonical identity is not proof that a card is playable or that its details exist.
- Exact query tracks/albums and accepted-artist catalog tracks/albums are separate inventories.
- Search snapshots may complete progressively, but already correct lead results must not be replaced by a lower-authority identity and already visible valid cards must not disappear.
- A snapshot revision represents a user-visible semantic change, not merely another completion attempt.
- Flutter may poll a pending snapshot, but it must not invent a top result, lead artist, containing album, or target-derived surface.

### Current deviation requiring correction

The travel branch has implemented most of the structure above, but the current code still starts MusicBrainz recording evidence only after the provider branches finish or time out. This can starve the evidence required to resolve ambiguous same-title tracks. The current Flutter decoder also falls back to promoting the first track when the backend returns no top result. These are known violations, not intended architecture; replace them in the existing path as described in `NEXT_STEPS.md`.

## Artwork

Track/album artwork and artist artwork are different domains:

- Track/album artwork may come from playable-source metadata or canonical release metadata.
- Artist artwork is keyed by canonical artist identity and served through `/artist_artwork/{key}`.
- Album and playlist artwork may be served through `/entity_artwork/{key}`, but the route is only transport. The object must be verified/persisted before the card is admitted.
- R2 is used when configured; otherwise artwork may remain in process/local cache.
- Entering an artist profile should persist newly resolved canonical artwork for later search and feed use.
- Entering an album profile must persist artwork through the same canonical entity-artwork path used by search; detail-only cache hydration is not sufficient.
- Flutter uses `cached_network_image` plus `flutter_cache_manager`; stale file paths must fall back to the network/backend URL rather than being treated as permanent success.

Do not fill an artist avatar with arbitrary album art merely to avoid a placeholder.

The current entity proxy is incomplete: it can create a proxy token from an unverified external URL, and the presence of that token currently satisfies artwork publication. This is a known bug. The corrected architecture persists verified bytes/state, bounded failure retry, canonical aliases, and snapshot notifications before publication.

## Playback

```text
canonical track key
    -> persisted source registry
    -> local prepared prefix / local cache
    -> R2 stream cache
    -> verified YouTube or Audius source
    -> yt-dlp stream resolution
    -> Flutter just_audio queue
```

Key backend modules:

- `api/media_runtime.py`
- `api/stream_runtime.py`
- `api/stream_core_runtime.py`
- `api/stream_cache.py`
- `discovery/source_registry.py`

Key client modules:

- `logic/playback_models.dart`
- `logic/audio_stream_runtime.dart`
- `logic/audio_queue_runtime.dart`

YouTube Data API provides metadata/video identities, not audio bytes. A YouTube result counts as playable only after the stream path can resolve it. Bot, age, geo, private, and unavailable failures should mark that source and permit an alternate source.

The C++ FFI library is packaged as ignored Android `.so` files under `app/android/app/src/main/jniLibs/`. The current app can use the Flutter/just_audio path when FFI is unavailable, but exact parity requires transferring or rebuilding those libraries.

## Persistence and Portability

| Data | Normal location | In Git? | Why it matters |
| --- | --- | --- | --- |
| Source code and tests | repository | Yes | Main implementation |
| `.env` | repository root | No | Provider, Supabase, R2, assistant configuration |
| Feed/candidate runtime | `runtime/recommendation_store.sqlite` | No | Existing user feed, inventory, search/source history |
| Assistant memory | `runtime/assistant_memory.sqlite` | No | Assistant context |
| Native Android libraries | `app/android/app/src/main/jniLibs/` | No | FFI parity on emulator/device |
| Flutter packages/build | `app/.dart_tool`, `app/build` | No | Re-creatable |
| Python environment | `.venv`/`venv` | No | Re-creatable; often not portable |
| Stream bytes | R2 or local cache | No | R2 survives machine changes |

For travel, the ZIP should include `.git`, `.env`, `runtime/`, and `app/android/app/src/main/jniLibs/`. Dependencies may be included, but Flutter and Python caches can contain absolute paths and may need regeneration.
