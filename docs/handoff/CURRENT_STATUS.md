# Current Status

Last reconciled: 2026-08-02

## Repository State

- Active branch: `travel/work`.
- Current committed head: `9fd0956 chore(app): finalize artwork cache and launcher assets`.
- `travel/work`, local `develop`, `origin/travel/work`, and `origin/develop` currently point to the same committed head.
- Earlier cross-stack checkpoints remain:
  - `c378092` — canonical backend discovery, search, source resolution, storage, and tests.
  - `30b0ae4` — Flutter feed, search, playback, details, and UI cutover.
  - `ac4ce49` — Neatie icon assets.
  - `9fd0956` — direct Flutter artwork-cache dependency and final launcher asset declarations.
- The working tree contains a large intentional travel-branch implementation: 22 tracked source/test/script files were modified before this documentation refresh, with roughly 3,800 additions and 1,500 removals.
- `AGENTS.md` and `docs/handoff/` are untracked until committed. `resources/app_icon.png` and `resources/neatie_3rd.png` are loose duplicate design sources and are not runtime assets.
- Do not reset, revert, or bulk-discard the working tree. Split it into the logical commits described in the handoff/final commit plan.

## Product Baseline Before This Travel Branch

Neatie is an Android-first Flutter client backed by a local Python/FastAPI service. The committed baseline already had one production recommendation path, one canonical search service, and one playback/source-resolution path. Replaced recommendation precompute systems had been removed.

The backend recognizes ten home rows:

1. `todays_pick`
2. `featured_new_albums`
3. `last_played`
4. `frequently_listened`
5. `made_for_you`
6. `because_you_played`
7. `popular_radio`
8. `recommended_albums`
9. `recommended_artists`
10. `quiet_picks`

The persisted formats remain `feed-state-v2`, `candidate-inventory-v6`, `discovery_home_artifact_v7`, and `artist-radio-inventory-v1`. They are serialization identifiers within one current implementation, not competing engines.

Previously device-proven behavior included a full ten-row persisted feed, materially larger reserves, better refresh variety, twelve Made For You cards, and working Popular Radio and album rows. That recommendation/feed baseline was not the focus of the travel-branch changes.

## Travel-Branch Objective

The branch began as the final device validation of canonical search, artwork, progressive enrichment, and source playability. Repeated real-device audits exposed that the previous search stack still allowed three kinds of false success:

- a text-similar entity could be accepted before authoritative identity was resolved;
- a non-empty artwork path could be accepted even if no image bytes existed;
- a snapshot revision could advance even when the visible result did not improve.

The work therefore concentrated on replacing client-side search ownership, coalescing canonical/provider identities, bounding enrichment, enforcing fail-closed target resolution, and containing yt-dlp failures. Some of that direction worked; the latest audit also found several remaining structural gaps described below.

## What Was Implemented on `travel/work`

### 1. Flutter search authority and progressive surfaces

- Removed the Flutter search payload cache, stale-cache fallback, local selection override, and result promotion logic from `search_payload_runtime.dart` and `main_home.dart`.
- Kept only request coalescing for identical in-flight searches. The backend snapshot is now intended to be the source of truth across repeats and enrichment.
- Added bounded progressive artist-surface polling at increasing delays. The client requests the existing backend snapshot rather than launching a second search engine.
- Merged progressive diagnostics and added one client log containing snapshot revision, related-artist count, artist-track count, artist-album count, query-album count, and playlist count.
- Kept exact query tracks separate from selected-artist tracks instead of mixing both inventories into the main Songs row.
- Added separate `Albums by <artist>` and `More from <artist>` sections.
- Moved related artists to the bottom of Top results and added progressive card/section entry animation with reduced-motion support.
- Changed artist-card artwork to `BoxFit.cover` so portrait/landscape provider images fill the square card.
- Filtered album and playlist grids when the payload contains no artwork string.

Important remaining flaw: `SearchPageState.fromJson` still manufactures a track top result from the first returned track when the backend sends no accepted target. This is why the latest `In Bloom` screen looked correct even though the backend explicitly returned `mixed` with no target.

### 2. Canonical classify-then-resolve search

- Added stable provider identity helpers for tracks, artists, and albums.
- Replaced text-first target construction with a classify-then-resolve bundle containing entity type, selected item, credited/lead artist, containing album, stable target identity, confidence tier, decision margin, and evidence.
- Locked identity resolution to the entity type selected from unconditioned provider evidence.
- Required independent MusicBrainz/provider agreement before accepting a same-title recording credit.
- Made unresolved competing recording credits fail closed to a neutral `mixed` page rather than building an artist catalog around a guess.
- Preserved candidate-order invariance and added tests for same-title recordings, famous/obscure homonyms, track/album distinction, typos, and provider-only classification.
- Kept exact query tracks/albums separate from selected-artist catalog tracks/albums during snapshot completion so an enrichment refresh cannot intentionally replace one inventory with another.
- Added accepted artist bridges so MusicBrainz and provider-backed records can merge through authoritative identity/alias evidence.
- Coalesced duplicate provider/MusicBrainz artist records in the persisted catalog and migrated their aliases, sources, and metrics to the accepted canonical record.
- Preserved explicit provider artist IDs during normalization instead of misclassifying MusicBrainz/derived IDs as provider detail IDs.
- Reused existing provider-backed artist catalog records and bounded first-page related-artist metadata resolution to three unresolved artists per pass.

What went wrong: recording evidence is still scheduled only after the provider branches finish or time out. The provider branch budget is approximately 5.5 seconds and the total retrieval budget approximately 6.5 seconds, leaving MusicBrainz at most 1.5 seconds. This starves the authoritative recording evidence needed for ambiguous queries. The latest `In Bloom` request therefore failed closed even though the provider's first track and MusicBrainz data can identify Nirvana.

### 3. Artist and entity artwork

- Strengthened artist artwork ownership: provider-backed homonyms cannot inherit an old normalized-name artwork object.
- Validated persisted `/artist_artwork/{token}` paths with the configured object store before reusing them.
- Preserved a real external source URL when a stale internal artwork token is rejected.
- Added positive R2 token caching to reduce repeated object-store `HEAD` calls.
- Added artist metadata update listeners so successfully cached artist artwork can refresh stored search snapshots.
- Related artists now require a provider detail identity and a verified `/artist_artwork/` object before becoming visible.
- Added a backend `/entity_artwork/{token}` route for album and playlist artwork, with alternate source URLs and R2 reuse.
- Tightened album publication to require detail readiness and an artwork value; playlist publication requires artwork.

What worked: Dio's main artist artwork appeared after boto3 was installed and identity coalescing was added. A later Dio pass showed a healthy related-artist section with artwork. Artist homonym/cross-contamination handling is materially stronger.

What did not work: album/playlist proxy attachment changes any non-empty source URL into `/entity_artwork/{token}` before the source is downloaded or verified. Publication then treats that proxy string as artwork success. If every source is dead, the image request returns 404 and Flutter shows a placeholder. There is no persisted background entity-artwork state or snapshot notification comparable to artist artwork. Album detail opening performs a fresh `get_album()` and stores only its detail cache, explaining why artwork can appear only after tapping the album.

### 4. yt-dlp and background source verification

- Broadened the yt-dlp selector from an m4a-preferred selector to any real audio-bearing format.
- Added a one-time unprocessed format inspection and selects the strongest actual audio URL if yt-dlp reports `Requested format is not available`.
- Reused cookies and quiet logging options for stream resolution and downloads.
- Suppressed raw per-video yt-dlp warnings in favor of aggregated source-registry diagnostics.
- Reduced adaptive background verification from 32–48 attempts to 8–16 attempts per cycle.
- Opened a five-minute provider circuit after three systemic format-unavailable failures, while retaining the existing longer block for bot/source-blocked failures.
- Kept user-requested playback independent from the background verification circuit.

This worked in the latest audit: the old 48-line format-error storm became four attempts, three format-unavailable results, and an open circuit. This prevents one bad provider state from consuming the whole background cycle. It does not guarantee that every YouTube source is playable, and cookies can reduce bot challenges but cannot eliminate them.

### 5. Development setup and logs

- `start_dev.ps1` now starts uvicorn with `--no-access-log`.
- It attempts to raise the Android `EGL_emulation` tag to warning before `flutter run`.
- The new Windows device was configured with a Google APIs x86_64 API 35 emulator, hardware acceleration, four CPU cores, and 4 GB RAM.
- PowerShell execution policy and Flutter discovery/PATH issues were addressed interactively.
- boto3 was installed on the new device, restoring configured R2-backed artwork/stream cache access.
- `AURALIS_YTDLP_COOKIES_PATH` was documented as a separate ignored machine-specific path.

The logging change is only partial. The latest Flutter terminal still contains `D/EGL_emulation`, `D/FlutterJNI`, IME, frame tracker, HWUI, and other Android debug output. Flutter recommendation code also emits request-body timing, request-start, timing, and a large diagnostics payload for a single successful request. Snapshot revisions currently generate additional logs without visible changes.

## Device-Test History on This Branch

### Pass A — Dio before enrichment fixes

- Main artist artwork was missing.
- Only two related artists appeared and both lacked artwork.
- Repeating the query returned the same weak cached result.
- Progressive enrichment never visibly updated the page.
- Root findings: boto3 was missing on the new machine, dead artwork URLs counted as valid, related artists used weak artwork admission, and Flutter was not polling the server snapshot.

### Pass B — Dio after artist identity/artwork changes

- Main Dio artwork appeared.
- Related artists became populated and visually relevant.
- Several provider portraits did not fit square cards; the Flutter artwork fit was changed to cover.
- Multiple Dio albums still had missing artwork.
- The requested result ordering was implemented: artist albums now precede the regular album row, while related artists appear last.

### Pass C — `In Bloom` before the final classifier replacement

- Search sometimes treated the query as a literal artist or chose a same-title low-authority artist.
- One result combined the wrong artist name with Nirvana artwork, proving identity and artwork had been merged independently.
- Artist tracks/albums/related artists were then built around the false target.
- A first classifier correction improved type detection but still selected The Butchers as the credited artist and built `Albums by The Butchers`.

### Pass D — Eric Clapton and Queen

- `Eric Clapton` resolved the correct artist and produced a useful catalog, but some artist albums and regular albums lacked artwork.
- `Don't Stop Me Now` correctly resolved Queen, but its selected-artist album surface was thin.
- A later progressive snapshot caused an existing `More from Queen` surface to disappear, showing that snapshot updates were not yet monotonic.

### Pass E — latest audit (`In Bloom` and Michael Jackson)

- Backend `In Bloom` diagnostics: `query_intent=mixed`, empty target identity, confidence `0`, and evidence `uncorroborated_competing_recording_credits`.
- Flutter still showed `In Bloom — Nirvana` as Top result because it promoted the first track locally.
- With no backend-accepted target, lead artist, containing album, artist tracks, artist albums, related artists, and playlists all remained empty.
- Snapshot revisions advanced from 8 to 16 with identical visible counts.
- `Michael Jackson` resolved correctly with confidence `0.99`, 38 artist tracks, 12 artist albums, and 5 playlists in backend diagnostics.
- Some Michael Jackson albums and regular query albums still lacked artwork; opening an album could make its artwork load afterward.
- Michael Jackson related artists remained at zero through multiple increasing revisions.
- Query latency remained significant: roughly 9.3 seconds of canonical search work for `In Bloom` and 6.9 seconds for Michael Jackson, in addition to the direct YTMusic timings reported separately.

## Current Resolution Matrix

| Handoff concern | Current status | Evidence |
| --- | --- | --- |
| Thin/two-row home feed | Device-proven resolved for the established account | Full ten-row persisted feed observed before travel search work |
| Overlapping recommendation implementations | Resolved structurally | Old precompute paths removed in committed backend cutover |
| One canonical `/search` path | Resolved structurally | Flutter cache/selection overrides removed; one backend snapshot path remains |
| Same-title/wrong-artist target building | Improved but not resolved | Backend now fails closed, but canonical evidence can be starved and Flutter fabricates a top result |
| Main artist artwork | Partially resolved | Dio and Michael Jackson main identity/artwork improved; persistence depends on valid provider/R2 evidence |
| Related-artist quality/artwork | Partially resolved | Dio became good; Michael Jackson remained empty because strict artwork/provider admission never completed |
| Album/playlist artwork | Not resolved | Optimistic entity proxy can publish dead sources; detail hydration is isolated |
| Empty/unplayable album cards | Improved, not device-proven resolved | Detail-readiness gate exists, but artwork/playability must be re-audited after the final fix |
| Progressive live enrichment | Polling implemented, behavior not resolved | Client polls; backend revisions can be no-ops and unresolved targets cannot enrich artist surfaces |
| Disappearing progressive sections | Not resolved | Queen surface disappeared in an earlier pass; semantic/monotonic snapshots are still missing |
| yt-dlp 48-error storm | Resolved/bounded in latest device evidence | Three systemic format failures open the circuit; cycle capped at sixteen |
| YouTube bot/format availability | Mitigated, not eliminated | Cookies, quiet logging, alternate selection, and circuits help; upstream access remains external |
| Search latency | Not resolved | Latest canonical lookup remained 6.9–9.3 seconds |
| Development log readability | Partially resolved | Uvicorn access log removed; Android and Flutter success logs remain noisy |
| Native FFI parity on new device | Not resolved | Packaged `libauralis.so` lacks `auralis_get_position`; app falls back to safe no-op mocks |
| Two-feed ready queue | Not started | Still blocked behind the search/artwork device gate |
| Silent later queue items | Not revalidated | Playback continuity audit still pending |
| `server.py` and oversized core files | Not started | Intentional structural-debt work after behavioral gates |
| Dormant Flutter keyword home-lane fallback | Not started | Remove after higher-priority behavior gates |

## Verification State

- Last inherited broad focused-backend result: `127 passed` before the later travel-branch corrections.
- The new search/source tests cover identity bridges, bounded related resolution, R2 artwork ownership, stale artwork paths, same-title credit handling, candidate-order invariance, conditioned-search rejection, the sixteen-item source cap, the format-failure circuit, and real-format fallback.
- On 2026-08-02 Codex attempted the combined focused command for the four modified test files. It could not start because neither `python` nor an installed interpreter through `py` is available in the Codex shell environment. Therefore there is no current automated pass count for this exact working tree.
- Do not repeat the stale claim that all final search/artwork tests passed. Repair/activate Python and run the focused commands in `NEXT_STEPS.md`.
- The user has performed multiple real-device builds and searches, proving the app compiles/runs on the AVD. The FFI symbol warning is a runtime native-library mismatch, not a Dart compile failure.
- Flutter analysis was not run by Codex and must be run by the user locally, as required by `AGENTS.md`.

## Immediate Priority

Do not begin the two-feed ready queue or structural cleanup yet. Replace the remaining flawed behavior in the existing search/artwork path:

1. Start canonical recording evidence early enough to finish, but consume it only after provider-only entity classification.
2. Delete Flutter's first-track top-result fallback.
3. Carry canonical artist, recording, release, and release-group identity into the accepted target; bind the containing album to a provider-backed usable detail record.
4. Replace optimistic album/playlist proxy admission with persisted verified entity artwork and background notification through the existing snapshot mechanism.
5. Make snapshot revisions semantic and progressive updates monotonic.
6. Consolidate successful search/recommendation logging and apply a real Android tag filter.
7. Run focused backend tests, user-run Flutter analysis, then the device audit in `NEXT_STEPS.md`.
