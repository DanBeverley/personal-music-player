# Known Issues and Guardrails

Last reconciled: 2026-08-02

## P0: Canonical Track Resolution Can Be Starved

The classify-then-resolve architecture is directionally correct: provider evidence locks the entity type and independent canonical evidence resolves identity within that type. The current scheduling is wrong.

`retrieve_search_candidates_fast` waits for the provider branches before submitting the MusicBrainz recording lookup. With the current branch/total budgets, canonical evidence may receive at most 1.5 seconds after several seconds have already elapsed. The latest `In Bloom` request therefore returned:

- `query_intent=mixed`;
- empty target identity;
- confidence `0`;
- `uncorroborated_competing_recording_credits`.

The provider's first track happened to be Nirvana, but the backend correctly refused to build target-derived surfaces without corroboration. Fix scheduling in the existing resolver: start canonical evidence early, consume it only after type lock, and record whether it hit, timed out, returned empty, or remained ambiguous.

Do not solve this with a title/artist-conditioned provider query that confirms its own premise. Do not add a second search route.

## P0: Flutter Still Fabricates a Top Result

`SearchPageState.fromJson` promotes the first normalized track whenever the backend returns no `top_result`. This made the latest neutral `In Bloom` response look like an accepted `In Bloom — Nirvana` target.

Consequences:

- the UI contradicts backend target diagnostics;
- a lucky first row appears authoritative;
- users cannot distinguish a fail-closed neutral result from a resolved target;
- future client logic can accidentally rebuild target-derived behavior around unaccepted identity.

Delete the fallback. The backend target bundle is the only authority for Top result, lead artist, containing album, and selected-artist expansion.

## P0: Accepted Tracks Do Not Yet Carry Complete Release Identity

The canonical track resolution currently retains recording identity and album text but does not consistently carry MusicBrainz artist, release, and release-group IDs into the final target. `resolve_search_target` derives the containing album primarily from the selected provider track.

For a track query such as `In Bloom`, the required result is:

- accepted recording;
- accepted credited artist;
- provider-backed artist detail identity;
- authoritative containing release/release group;
- provider-backed album detail identity and usable persisted tracklist.

Carry canonical release evidence through resolution, match it to already-fetched provider albums, and use conditioned album lookup only as hydration after the track is accepted.

## P0: Album and Playlist Artwork Is Optimistically Admitted

The travel branch added `/entity_artwork/{token}` and alternate-source download logic. However, `attach_entity_artwork_proxy` rewrites any non-empty source URL to an internal proxy before image bytes are verified. Album/playlist publication sees the non-empty proxy string and accepts the card. If every source fails later, the route returns 404 and Flutter shows a placeholder.

This is the remaining form of the original “dead URL treated as valid” bug. It moved the failure behind a proxy instead of removing it.

Required replacement:

- verified persisted entity-artwork state keyed by canonical album/playlist identity;
- bounded alternate-source resolution and failure retry;
- no publication based solely on a proxy token;
- snapshot notification after verified bytes become available;
- album detail hydration writing to the same canonical state.

Avoid blocking per-card R2 `HEAD`/download fan-out in the normal search response.

## P0: Progressive Enrichment Is Not Yet Semantic

Flutter now polls pending artist surfaces, which fixed the earlier absence of a client refresh loop. The backend still unconditionally increments snapshot revision after completion attempts.

Latest evidence:

- `In Bloom` advanced revision 8 to 16 with zero artist tracks/albums/related artists/playlists throughout.
- Michael Jackson advanced revisions 4, 11, and 12 with identical visible counts.
- An earlier Queen test lost a previously visible `More from Queen` surface after a later snapshot.

Required behavior:

- revision advances only when the user-visible fingerprint changes;
- enrichment merges monotonically and does not remove already visible valid cards;
- exhausted/terminal surfaces stop polling;
- client logs only visible changes.

## Search Completeness and Relevance

Exact artist searches are much stronger. Michael Jackson resolved at confidence `0.99` with a large artist catalog and playlists. Remaining completeness risks include:

- related artists can remain empty because visibility requires both provider identity and verified R2 artwork, while bounded background resolution may never complete enough candidates;
- regular Albums for artist queries can contain tribute, relaxation, sleep, or cover compilations whose metadata mentions the artist but whose accepted artist relationship is weak;
- artist albums may be thin when playable/detail/artwork gates remove most of the canonical discography;
- neutral track queries cannot populate lead-artist surfaces until target identity resolves.

Keep `Albums by <artist>` identity-bound. If regular query-matching albums remain, label and rank them as other matches rather than presenting them as the artist's discography.

## Search Latency

Latency remains a P0 quality concern:

- latest `In Bloom` canonical lookup: about 9.3 seconds;
- latest Michael Jackson canonical lookup: about 6.9 seconds;
- direct YTMusic was separately about 2.4 seconds and 1.4 seconds respectively;
- optional artist catalog, relationships, playlists, album tracklists, and artwork can extend work.

Start independent evidence concurrently, return the first correct useful payload, and finish optional verified surfaces through persisted snapshots. Do not trade correctness for a fake instant target.

## Artist Artwork Consistency

Artist artwork ownership is materially improved:

- provider-backed homonyms no longer inherit legacy normalized-name R2 objects;
- stale `/artist_artwork/` tokens are verified;
- a valid external source URL can recover after a stale token;
- Dio main/related artist artwork worked in a later device pass.

Still required:

- confirm reuse after backend restart, not only within one process;
- ensure strict related-artist gates eventually complete rather than leaving a permanent empty section;
- never substitute album/track art as authoritative artist art;
- preserve complete existing radio artwork while a replacement is incomplete.

## yt-dlp and YouTube Availability

The former 32–48 item background format-error storm is now bounded:

- 8–16 adaptive verification cap;
- broader audio selector;
- one real-format fallback;
- quiet per-video logging;
- circuit after three systemic format failures;
- existing circuit after two bot/source-blocked failures.

The latest device log showed four attempts, three format failures, and `circuit_open=1`, so containment works. This does not guarantee YouTube access. Cookies can reduce bot challenges but can expire, become invalid, or fail to affect format availability. Keep user-requested playback independent from the background circuit and preserve alternate verified sources.

## Development Log Noise

`--no-access-log` removed normal uvicorn access lines. The current `EGL_emulation` property change did not clean the Flutter terminal sufficiently.

Remaining noise:

- Android `D/EGL_emulation`, `D/FlutterJNI`, IME, FrameTracker, HWUI, Choreographer, and profile-installer messages;
- Flutter recommendation request-body timing;
- separate request-start, timing, and full diagnostics success lines;
- progressive logs for no-op revisions;
- repeated R2 stream-cache enabled messages from multiple service wrappers/processes.

Use an `ANDROID_LOG_TAGS` filter around the Flutter process, one compact recommendation success line, one compact search line, and error/contract diagnostics. Do not hide warnings or failures.

## Native FFI Parity on the New Device

The app reports that packaged native symbols do not match `libauralis.so`; `auralis_get_position` is missing. The app falls back to safe no-op mocks, so this is not a Dart compile failure, but exact native-engine behavior is unavailable.

The `.so` files are ignored by Git and machine/archive-specific. Transfer compatible `arm64-v8a` and `x86_64` libraries or document/rebuild them from `core_native/`. Do not assume a copied binary matches the current Dart FFI contract.

## Playback Start and Queue Continuity

Source persistence, prefix preparation, R2 hit-first behavior, and queue source pre-resolution exist. Remaining device concerns:

- first uncached playback may exceed one second;
- earlier audits showed the first queue track playing while second/later tracks advanced silently;
- native FFI mismatch can change behavior on the travel device;
- bot/age/geo/private/unavailable sources still affect first-time resolution.

Revalidate first, second, and third automatic items; manual skips; a second queue from Search; and R2 hit versus uncached source.

## Feed Rotation and Refresh

The established account's feed completeness improved and a full ten-row persisted feed was observed. The lifecycle still uses one prepared slot.

Agreed future replacement, blocked behind the search/artwork gate:

- remove forced refresh merely because the user returns from Search;
- maintain at least two ready future feeds;
- build backups from retained canonical inventory plus genuinely new acquisition;
- pull refresh promotes a ready feed immediately and schedules replenishment;
- Flutter presents server-approved versions without adding a client feed authority.

## Popular Radio

Popular Radio remains independent from normal row allocation and publishes only from playable artist catalogs. Remaining concerns:

- canonical seed artwork consistency;
- independent rotation/fingerprint;
- enrichment from twelve toward twenty-four tracks;
- distinct visible cards and low duplication with main shelves.

## Metadata, Artist Profiles, Lyrics, and Search Landing

- Release-year coverage is incomplete; never substitute upload year without evidence.
- Artist profiles do not yet guarantee a complete canonical public discography with per-recording playability.
- Behind the Lyrics can still emit generic interpretations; distinguish model/provider failure from genuinely low context.
- Charts/Trending/search landing cards remain placeholders or shallow shortcuts and need researched live providers and canonical playable drill-down.

## Portable State Risks

These are ignored and must be moved separately:

- root `.env`;
- `runtime/recommendation_store.sqlite`;
- `runtime/assistant_memory.sqlite`;
- `app/android/app/src/main/jniLibs/`;
- yt-dlp cookies;
- optional local environments/caches.

`AURALIS_YTDLP_COOKIES_PATH` is an absolute machine path and must be updated after transfer. The current Codex shell has no usable Python interpreter on PATH or through `py`; activate/rebuild Python before claiming backend verification.

## Build and Repository Hygiene

- Active branch is `travel/work`, not `develop`.
- `start_dev.ps1` targets `emulator-5554`; `10.0.2.2` is only for an emulator reaching the Windows host.
- Release APKs currently use debug signing.
- `resources/app_icon.png` and `resources/neatie_3rd.png` are untracked duplicate design sources; runtime branding is under `app/assets/branding/`.
- `server.py`, several backend/Flutter core files, and the dormant Flutter keyword home-lane fallback remain structural debt. Address them only after behavior gates pass.

## Approaches Not to Repeat

- Adding a second recommendation/search implementation.
- Restoring Flutter search caches, selection overrides, or target inference.
- Treating a proxy URL, token, or non-empty string as verified artwork.
- Treating MusicBrainz identity as playable audio/detail readiness.
- Using a title/artist-conditioned search to prove the identity it was conditioned on.
- Blocking the first payload on every optional surface.
- Advancing snapshot revision without a visible semantic change.
- Removing valid visible sections during enrichment.
- Hardcoded artist, song, language, title, or query-specific blocklists.
- Broad feed queries such as `artist top songs`.
- Publishing partial album/radio cards merely to make a row appear.
- Claiming a device issue is resolved only because fixtures passed.

## Non-Negotiable Product Requirements

- No thin two-row feed as the normal experience.
- Search must identify the correct canonical entity before target-derived expansion.
- A neutral result must be honestly neutral.
- Album/artist-work cards must have verified artwork and usable content.
- Progressive enrichment improves monotonically and visibly.
- Discovery remains unplayed, taste-connected, canonical, and source-quality aware.
- Popular Radio remains independent from regular allocation.
- Replacements remove flawed logic instead of layering another fallback over it.
