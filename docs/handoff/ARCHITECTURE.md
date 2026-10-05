# Architecture

October 3 clarification: history response construction recovers exact persisted catalog/entity-source metadata; bounded provider repair runs in background. Playback evidence is retained independently from display readiness. Shared Home admission requires usable title/artist even for trusted history, and bundle schema v2 invalidates older placeholder-containing bundles for recomposition. Personal mix slices are multi-card ranked reservoirs; diversity is enforced within each card, with unique visible recording sets and bounded deep overlap. Search revision timeouts acknowledge `not_modified` without replacement surfaces; only actual revisions replace client results. See [deployment and verification](CURRENT_STATUS.md).

Last reconciled: 2026-10-04. Current ownership/contracts are retained here; diagnostic deviations belong in KNOWN_ISSUES and remaining work in NEXT_STEPS.

## Current System Shape

```text
Flutter Android client + separate Flutter web client
        |
        | HTTP / JSON
        v
FastAPI Python proxy
        |
        +-- canonical Search
        +-- Home repository API and promotion
        +-- playback/source/stream endpoints
        +-- Charts, New Releases, Lyrics, Assistant, Recognition
        |
        +----------------------+
                               |
                               v
                  Recommendation worker process
                    +-- candidate acquisition
                    +-- Popular Radio acquisition
                    +-- artist artwork repair
                    +-- immutable feed composition

Durable state: SQLite runtime repositories, optional Supabase/Postgres sync
Durable objects: Cloudflare R2 stream/artwork cache when configured
Providers: YTMusic/YouTube, Last.fm, ListenBrainz, MusicBrainz, optional Audius
```

`start_dev.ps1` starts three visible responsibilities:

1. Recommendation worker — owns slow acquisition, verification, artwork repair, and feed composition. Most `[EBB:acquisition]`, `[EBB:source-registry]`, and `[EBB:home-worker]` logs belong here.
2. Python proxy/FastAPI (“A-terminal”) — owns HTTP request handling and therefore now mostly shows requests and endpoint logs.
3. Flutter terminal — owns application/device logs.

Background work moved out of the API process deliberately. It prevents slow provider operations from blocking `/recommend` responses and makes jobs durable across request boundaries. It is not evidence that the backend stopped working.

For the Android emulator, the host backend is normally `http://10.0.2.2:8000`. `127.0.0.1` inside Android points to the emulator, not Windows.

## Android Client

### Application Structure

- Main working tree: `app/lib/main.dart` owns Android bootstrap. In the web worktree it conditionally selects `main_android.dart` or `web/web_app.dart`.
- `app/lib/main_shell.dart` — authenticated navigation shell and global mini-player placement.
- `app/lib/main_home.dart` — Home and Search entry points, refresh, row paging, Charts and New Releases navigation.
- `app/lib/main_details.dart` — album, artist, mix, radio, and collection detail routing.
- `app/lib/main_player.dart` — full player.
- `app/lib/main_library.dart` — library surfaces.

### Important State and Runtime Modules

- `logic/audio_provider.dart` — central playback-facing state and shared exports.
- `logic/audio_provider_recommendation.dart` — launch/refresh/queue-status Home requests and backend diagnostics.
- `logic/home_feed_cache.dart` — scoped offline/crash recovery for an accepted backend artifact.
- `logic/audio_provider_queue.dart` — queue/session ownership and related-song replenishment.
- `logic/audio_queue_runtime.dart` — identity-based queue lookup and stall recovery decisions.
- `logic/audio_stream_runtime.dart` — stream resolution/preparation and cache/source selection.
- `logic/playback_models.dart` — canonical and playable source payloads.
- `logic/details_provider.dart` — details, lyrics, and Behind the Lyrics requests.
- `logic/search_provider.dart` and `logic/search_payload_runtime.dart` — canonical backend Search state/decoding.
- `logic/charts_provider.dart` — chart filters, snapshots, preparation, and results.
- `logic/new_releases_provider.dart` — New Releases state.
- `logic/history_manager.dart` — local history plus backend synchronization.
- `logic/track_metadata.dart` — shared canonical/display metadata helpers.

Flutter does not decide Home quality or independently promote a feed. It may validate account scope/version and retain the last valid artifact for offline recovery. The backend is the sole authority for active/ready promotion.


## Web Client and Shared Backend

Android and web are separate clients sharing backend contracts and cloud accounts. This is not a wholesale Android UI port. Backend improvements benefit both only after both use the same deployed version and endpoint; client-specific playback behavior remains separate.

In the web worktree, `app/lib/main.dart` conditionally imports the platform entry point. Web modules under `app/lib/web/` own:

| Module | Responsibility |
| --- | --- |
| `browser_audio.dart` | HTML audio, browser media events, Media Session controls and session storage |
| `web_player.dart` | Selected/loaded source ownership, queue, preparation, reconciliation and diagnostics |
| `music_api.dart` | Shared HTTP transport, authentication and account scope |
| `web_auth.dart` | Browser Supabase session, email/Google login and recovery |
| `web_library.dart` | Account-scoped cloud likes and playlists; gated targeted playlist writes |
| `catalog_api.dart` | Canonical search, recommendation and detail contracts |
| `catalog_screen.dart` | Search and Home, authoritative revision replacement, bounded polling and separate paged inventory |
| `catalog_detail.dart` | Artist and album detail views |
| `home_widgets.dart` | Web-only Home cover/hero, circular artists, collection collages and track-strip presentation; no acquisition or readiness decisions |
| `account_screen.dart`, `library_screen.dart`, `web_app.dart` | Responsive account/library/navigation surfaces |

Browser playback reuses an already prepared source. Gesture-sensitive playback, media-button actions, foreground return and UI/queue reconciliation must agree on the same track, operation and audio element. Stored position/duration restore is local and account-scoped; it is not cloud device handoff.

Preparation is bounded and sequential: first search candidates and a limited queue lookahead. Actual playback takes priority over speculative preparation. This is not indefinite caching of the entire growing queue.

The Oracle browser-audio adapter is an optional compatibility layer, not a replacement source system. Bounded byte requests/retries preserve source identity. Capture completion follows expected-byte validation and pending writes, rather than waiting for playback to end; media validation precedes durable cache admission. Upstream cold preparation can still be slow.

Failure diagnostics retain source/session identity and useful failure history. Compact backend summaries distinguish queue wait, preparation and transfer. These mechanisms improve evidence and recovery; they do not establish that every reported iPhone failure is fixed.

Supabase remains the account/library authority. Existing likes/playlists can be loaded and played. Playlist mutation support is implemented but disabled by default (`WEB_PLAYLIST_WRITES=false`) pending Android sync rollout and concurrent-edit validation. Google/email are enabled; GitHub remains hidden unless explicitly configured. See the acceptance gates in [NEXT_STEPS.md](NEXT_STEPS.md).

The outer Caddy handles HTTPS and routes to the API/static preview gateway. The web gateway exposes the existing search/playback/catalog/recommendation contracts. Installability, offline downloads and explicit cross-device handoff remain future work.

## Home Feed Architecture

October 4 deployed H10–H14 presentation: the web shell forwards visibility/tab activity to Home. A return checks existing queue status without long polling, at most once per 30 seconds and with in-flight coalescing; populated rows/scroll stay stable until explicit refresh. Empty Home can recover the ordinary backend artifact. Backend diagnostic lanes remain the sole lane contents; the client does not infer lanes from titles or refetch providers. The album hero combines persisted qualified album rows, respects reduced motion/inactive Home and only plays supplied embedded previews within the gesture; otherwise it opens persisted detail. Home likes consume the existing scoped library. Track options and collection details use existing player/catalog callbacks; playlist writes remain gated. CURRENT_STATUS owns the serving build; device acceptance is pending.

October 5 visual layer: `app/lib/web/design_system.dart` owns the web palette, typography and themes, with locally bundled Manrope/DM Serif Display and OFL licenses. `home_widgets.dart` owns the daily pick, album spotlight and artist/card/track presentation; `editorial_placeholder.dart` keeps missing covers explicit after exact alternatives fail. `web_app.dart` opens Home and selects a sidebar or bottom navigation at 850px. `playback_dock.dart` consumes the existing WebPlayer/BrowserAudio: the desktop dock and phone modal control one audio element. Seek, repeat, queue, likes, loading and interruption messages retain their callbacks. Hover/carousel motion respects reduced-motion settings. Account, library and details retain data/write gates. This layer does not acquire provider data, admit inventory, promote feeds or change source verification.

The old `discovery/feed_state.py` and `discovery/service.py` lifecycle has been removed. Current Home ownership lives under `auralis_backend/home_feed/`.

### Package Responsibilities

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Immutable row, radio, snapshot, state, artifact, bundle, and job contracts |
| `repositories.py` | Durable SQLite repositories, schema, immutable writes, leases, compare-and-swap state |
| `candidate_supply.py` | Builds/merges the account candidate snapshot from persisted inventory and bounded acquisition |
| `radio_supply.py` | Sole radio acquisition/composition owner plus independent artist-artwork repair |
| `composer.py` | Pure snapshot-to-artifact composition and durable artifact/state commit |
| `worker.py` | Claims durable jobs, renews leases, runs isolated worker lanes, retries only useful work |
| `scheduler.py` | Idempotent/coalesced candidate, radio, artwork, and compose scheduling |
| `api.py` | Serves/promotes active and ready artifacts, row pages, queue status, and replenishment requests |
| `bundles.py` | Immutable row/detail bundle support |

### Durable Supply Flow

```text
account history/library/search/feedback
        -> stable account Home supply profile
        -> provider-neutral acquisition plan
        -> YTMusic / Last.fm / ListenBrainz / optional MusicBrainz calls
        -> canonical merge and semantic deduplication
        -> playable-source verification
        -> retained verified candidate inventory
        -> immutable candidate snapshot
```

The supply profile is stable per account. Launch tokens, request seeds, and temporary history fingerprints influence ranking but do not create competing candidate stores.

MusicBrainz provides canonical artist/release/recording evidence. It does not provide audio and is not a Home readiness dependency. YTMusic, Last.fm, and ListenBrainz can still contribute useful supply when MusicBrainz is unavailable. Successful results are persisted before another provider's retry.

### Fresh Versus Usable

- Candidate snapshot TTL: 8 hours.
- Radio snapshot TTL: 8 hours.
- Ready artifact TTL: 8 hours.
- Active artifact freshness: 24 hours.
- Accepted stale-active display fallback: at most 3 days.
- Discovery exposure cooldown: 24 hours.

`fresh` answers whether a replacement should be scheduled. `usable` answers whether verified stored data can still safely build or display something while refresh runs. Expiry must never delete or invalidate all useful verified candidates merely because eight hours passed.

### Background Jobs and Progress

There are four durable job types:

- `refresh_candidates`;
- `refresh_radio`;
- `repair_artwork`;
- `compose_feed`.

Jobs use leases, idempotent generation keys, coalescing, retry/backoff, and compare-and-swap state. Provider acquisition may be slow, but it is isolated from local composition/artwork lanes. Progress means useful persisted supply, verified artwork, or a published artifact—not merely scheduling the same request.

The composer consumes only persisted snapshots. It does not call MusicBrainz or YTMusic directly. It uses allocation epochs, content fingerprints, exposure history, and supply watermarks to prevent duplicate workers from repeatedly publishing the same rotation.

### Feed Lifecycle

```text
candidate snapshot + optional complete radio snapshot
        -> pure row composition
        -> row and artifact contracts
        -> immutable artifact/bundles
        -> compare-and-swap commit
        -> active artifact + two ready successors
```

- Queue target depth is 2.
- Launch serves active immediately and may promote one ready artifact once per launch token.
- Pull-to-refresh promotes one ready artifact immediately when available.
- Promotion schedules successor replenishment.
- If no successor exists, active remains visible while work runs.
- A stale accepted active artifact may remain visible during recovery but is not counted as ready and cannot be promoted again.
- Incomplete artifacts never replace active.
- Initial publication waits for complete core supply. Once an accepted active feed exists, an incomplete independent radio replacement must not block improvements in other rows.
- The abandoned progressive/partial-row experiment is not the current architecture; it was reverted because it produced missing rows and empty states.

### Home Row Contracts

| Row | Requirement | Minimum | Target | Maximum | Reuse window |
| --- | --- | ---: | ---: | ---: | ---: |
| Today’s Pick | Core | 6 | 6 | 8 | None |
| Featured Albums | Independent | 4 | 8 | 10 | 24 h |
| Last Played | Earned | 8 | 8 | 16 | Evidence-owned |
| Frequently Listened | Earned | 8 | 8 | 16 | Evidence-owned |
| Made For You | Core | 5 | 8 | 12 | None |
| Because You Played | Earned | 12 | 12 | 48 | Evidence-owned |
| Popular Radio | Independent | 10 | 12 | 15 | 8 h |
| Recommended Albums | Independent | 4 | 12 | 48 | 24 h |
| Recommended Artists | Core | 10 | 10 | 48 | None |
| Quiet Picks | Core | 20 | 20 | 200 | None |

Earned rows appear only when the user has sufficient evidence. Independent rows cannot be fabricated or thinned below their own contract merely to show a title.

### Popular Radio

Popular Radio is not allocated as a normal track row.

```text
fresh/usable candidate snapshot
        -> related, exploratory, and familiar artist seeds
        -> radio-only multi-provider acquisition
        -> canonical playable artist catalogs
        -> independent artist artwork jobs
        -> immutable radio snapshot
        -> Home composer reference
```

Publication contract:

- 10–15 cards, target 12;
- at least 8 playable tracks per card, target 12;
- at least 3 non-mix discovery artist cards;
- at least 2 verified multi-artist mixes;
- at least 3 artist identities new relative to the previous rotation;
- artist cards require canonical identity and persisted `/artist_artwork/` artwork;
- mix cards require at least two distinct source artists and two verified artwork sources.

Artwork absence schedules artwork work and allows acquisition to advance to another seed. The radio worker is the sole owner of radio acquisition. The feed composer only consumes a persisted complete radio snapshot.

October 4 deployed H05–H08 corrections: radio acquisition projects identity-owned artist credits through shared admission; the compact index rematches every stored verified physical source against title/artist/version before joining playback. Readiness and publication share the non-mix discovery/depth contract. Artist thumbnail paths must match identity tokens; mix images must be distinct and belong to credited source identities. Novelty counts actual artists rather than synthetic mix IDs. Album planning applies its 16-artist bound after exhausted-provider filtering, rotates supported seeds and preserves zero cursor wrap and aggregate seed progress in the persisted ledger. Provider and source-verification failures remain distinct existing stages. Existing immutable artifacts are retained; fresh provider enrichment/complete supply remains an acceptance gate.

## Canonical Search

There is one production `/search` implementation in `auralis_backend/search/service.py`.

```text
normalized query
        -> local/direct retrieval and provider candidates
        -> entity-type classification
        -> type-gated canonical evidence
        -> accepted canonical target bundle
        -> artist/release/catalog expansion
        -> playable and detail validation
        -> persisted semantic snapshot
        -> progressive/paged Flutter surfaces
```

Important invariants:

- query text is evidence, not an identity assigned to every result;
- type classification precedes consumption of type-specific canonical evidence;
- the accepted target owns artist/release identity for dependent surfaces;
- canonical identity is not proof of playability;
- source IDs and canonical recording IDs remain separate;
- revisions advance only for visible semantic change;
- a newer semantic revision replaces its ranked first page and can retract invalid rows or clear null targets; separately loaded pages survive, and same-revision timeout acknowledgements preserve visible results;
- Flutter cannot invent a top result or substitute the first track when the backend has no accepted target;
- artwork is considered publishable only after persistent verification, not because a proxy token exists.

Search remains one canonical pipeline. The web branch adds confidence-based correction, original-query opt-out, stricter query-to-artist admission, alias learning/invalidation, and cached-target revalidation. Preserve uncertain valid names; do not create a separate web search engine.

## Playback and Related Tracks

```text
canonical requested track
        -> persisted source identities and alternates
        -> version/artist/title/duration compatibility ranking
        -> local prepared prefix or local cache
        -> Cloudflare R2 stream object
        -> live verified YouTube/Audius resolution
        -> Android just_audio queue / browser HTML audio element
```

YouTube Data and YTMusic metadata do not themselves prove stream playability. yt-dlp resolves the actual audio stream. YouTube may reject a source because of bot, age, region, privacy, deletion, format, signature, or token behavior; the registry must mark the failing source and try an eligible alternate.

For canonical studio tracks, source ranking rejects or heavily penalizes live, remix, cover, karaoke, tribute, instrumental, backing-track, sped/slowed, and similar version conflicts unless that version was explicitly requested. Artist/title/duration and release evidence must still align.

The Flutter queue uses logical track identity rather than assuming the audio engine index is always synchronized. Search playback creates a discovery playlist, `/related_tracks` pages recommendations, and near-end queue state triggers bounded replenishment. A temporary provider failure is retryable and must not become a permanent “No more related songs” state.

Stall recovery distinguishes a true unchanged playback position from normal buffering. It allows one bounded soft seek or source refresh rather than an endless loop.

Android release Gradle uses CMake and the `managedJniLibs` source set. Older ignored `app/android/app/src/main/jniLibs/` files may still exist in transferred archives, but copying them alone does not establish current release/native parity. Verify compiled ABI libraries and Dart FFI symbols on device; a mismatch can select the safe Flutter path.

## Charts and Trending

`auralis_backend/charts/service.py` owns persistent chart snapshots.

| Provider | Scope used by this product | Notes |
| --- | --- | --- |
| ListenBrainz | Sitewide and longer-range charts | Not falsely labeled regional |
| Last.fm | Current global/regional charts | Artwork may require independent enrichment |
| YTMusic | Global/regional chart playlists | Does not require YouTube Data API for enumeration |

Provider collection and playable verification are parallelized within bounded limits. Publication requires at least 10 playable entries. All Sources balances providers; source filters show that provider's available ranked list. Snapshots store prior rank so `is_new_entry` means absent from the prior comparable chart, not simply first seen by the UI.

Demand for recently selected country/range pairs is persisted and warmed in the background. The system should not preload every country indefinitely.

## New Releases

`auralis_backend/releases/service.py` and the Flutter New Releases screen replace the former Search redirect.

- YTMusic and ListenBrainz supply releases;
- refresh starts with a 45-day window and can extend to the configured 90-day release window;
- results are canonicalized, deduplicated, artwork/detail checked, persisted, and refreshed in the background;
- a visible release should open to a usable album detail and playable track backing.

## Lyrics and Behind the Lyrics

`auralis_backend/lyrics/service.py` uses:

1. YouTube Music lyrics for a resolved video/source;
2. LRCLIB exact and searched matches using normalized artist/title/album/duration evidence.

It distinguishes found, instrumental, provider failure, and not-found states and supports synchronized or plain text. Behind the Lyrics is a separate AI interpretation endpoint and still needs its own reliability/device gate. Long copyrighted lyric text must not be logged or unnecessarily persisted in interpretation records.

## Artwork Domains

- Track artwork belongs to a verified playable source or authoritative release.
- Album artwork belongs to an authoritative release identity and must be persisted before card admission.
- Artist artwork belongs to canonical artist identity and is served through `/artist_artwork/{token}`.
- `/entity_artwork/{token}` is transport for verified album/entity bytes, not proof that the object exists.
- Mixed radio artwork contains distinct verified artist sources.

Never use arbitrary track/album art as an artist portrait merely to remove a placeholder.

## Persistence and Deployment

| Data | Normal location | In Git? |
| --- | --- | --- |
| Source and tests | Repository | Yes |
| Provider/secrets configuration | Root `.env` locally or protected deployment env | No |
| Home/Search/source runtime | `runtime/` SQLite state | No |
| Assistant memory | `runtime/assistant_memory.sqlite` | No |
| Stream/artwork bytes | R2 or local cache | No |
| Android native libraries | CMake output / `managedJniLibs`; legacy ignored `jniLibs` may exist | Generated/local |
| Flutter/Python build caches | Local tool directories | No |

AWS currently runs Caddy → FastAPI + recommendation worker, a separate static web gateway, token provider and the optional browser-audio compatibility service in Docker. The adapter originated for Oracle but remains a transport option on AWS. Runtime/download directories and Caddy volumes persist separately from releases. See [OPERATIONS.md](OPERATIONS.md) for current host, paths, backups and rollback. The preview is independent of the PC.

## Architecture Guardrails

- One Home lifecycle: `home_feed`, not the deleted discovery feed service.
- One canonical Search path.
- One playback/source registry and stream path.
- Backend alone promotes Home artifacts.
- Customer requests consume persisted supply; broad provider work runs in the worker.
- Popular Radio remains independent from regular allocation.
- Canonical identity is never treated as proof of playability, detail readiness, or artwork bytes.
- Provider failures are isolated; successful persisted work survives.
- Fix root ownership/state errors; do not add success-looking fallback states.
- Replaced systems are deleted, not retained as parallel safety layers.
- No hardcoded artist/title/language blocklists as product logic.
