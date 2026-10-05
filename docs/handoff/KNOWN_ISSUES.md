# Known Issues and Evidence

October 5 frontend scope: the quiet minimal desktop-first redesign is committed at application checkpoint e4e7529. It addresses visual hierarchy, daily-pick artwork space, card/artist/track styling, navigation and compact/expanded playback controls. Automated layout/action checks and synthetic Chromium evidence do not close provider-artwork, fresh album/radio supply, real-account or iPhone gates. Earlier uncommitted source statements below describe their original checkpoint; H01–H14 and album transport were committed October 5. CURRENT_STATUS owns deployment evidence and OPERATIONS owns rollback.

Reconciled October 3, 2026. This file preserves diagnostic findings and limits; [CURRENT_STATUS.md](CURRENT_STATUS.md) records shipped changes and [NEXT_STEPS.md](NEXT_STEPS.md) owns all 66 unfinished/recheck/deferred entries. No application changes or live probes were performed during documentation consolidation. Historical findings are explicitly dated and must not be presented as unfixed current behavior without rechecking.

## Current Home artwork and interface findings (H01–H14)

October 4 follow-up: H10–H14 implement the presentation gaps described below: foreground queue checks, Today's Pick cover, animated Featured Albums, circular artists, distinct collages/cards, recent/frequent/like strips, backend lanes and collection/track actions. Build 2026-10-04-home-presentation is deployed. H09 adds repeat/reload/signal-read regression evidence; cloud/device acceptance remains open. The historical comparison below remains evidence of the October 3 baseline, not a claim that these UI omissions are still unfixed. Data/provider supply issues are independent and remain open.

Fresh read-only audit around 06:42–06:46 UTC confirms worker progress after H05–H08, but album publication remains defective: 49 requests, 98 raw album results, zero qualified. Of 1,250 canonical track occurrences only 23 have fresh compatible verified sources, and 42 of 98 albums have no source records. All 98 fail coverage; relevant source records include 150 stream_failed and two source_blocked failures. Acquisition/cursor/retry fixes passing tests do not prove this live boundary resolved; the precise provider/transport/scheduling cause still needs a bounded failure trace. Radio grew to seven reserve/six visible cards, but minimum depth/non-mix discovery still fail. No new audited-scope artifact has been composed after H05–H08. Do not lower publication contracts or claim the new hero fixes supply.

These observations describe the October 3 deployed baseline. H01–H04 source corrections were deployed October 4 as 2026-10-04-home-artwork, followed by H05–H08 as 2026-10-04-album-radio; old immutable artifacts were not rewritten. Fresh artifact/artwork/provider acceptance remains open.

### October 4 album transport root-cause trace (H04/H05)

The later authorized read-only audit found 110 unique acquired albums and 1,270 distinct canonical track keys, represented by 1,469 album-track occurrences. Only 237 occurrences have source records and 25 have fresh compatible verified sources; one additional occurrence has a verified but incompatible source. Forty-nine albums have no source records, and all 110 fail coverage. Relevant source records contain 152 stream_failed and two source_blocked failures. Occurrence counts include tracks repeated across editions; they are not distinct songs.

Two production transport defects are confirmed. Both API and recommendation worker receive readable cookies mounted read-only. The extractor passes that mounted path as yt-dlp cookiefile; installed yt-dlp 2026.8.19 saves its cookie jar on YoutubeDL.close. Bounded live extraction under both worker and API settings raised OSError with a cookie-write/read-only permission category, which classify_stream_failure reduces to stream_failed. Cleanup can obscure an earlier extraction failure. _verify_source persists only that generic code, losing exception class/stage/errno and masking the operational cause.

The worker also lacks AURALIS_YTDLP_PO_PROVIDER_URL and AURALIS_BROWSER_AUDIO_URL, both configured on the API. Missing AURALIS_YTDLP_JS_RUNTIME is not independently a defect: the server defaults to node and node exists in both containers. In a temporary diagnostic process, cookies were loaded into memory with cookiefile omitted, leaving the mounted master file unchanged. For the same failed source within that comparison, worker extraction settings then failed media preflight with source_blocked, while API client/token settings returned validated media. The existing browser-audio route also returned validated media in the earlier bounded probe. This establishes recoverable transport failure for the tested source; it does not establish that every stored failure shares this cause or that a complete album now qualifies.

Recovery remains limited to 16 new verifications per candidate batch, shared across pools. Recent worker batches attempted 8–16 album sources and verified zero across their whole batches. Generic stream_failed errors do not trigger the blocked-source circuit. No added usable identities increases candidate backoff up to one hour, and a generally ready inventory ends the refresh loop even while optional album supply is short. These are recovery/scheduling contributors, not a reason to weaken coverage.

Required solution: preserve the read-only master cookies but load an extraction-owned in-memory jar without save-back; give the worker the same existing client/token settings and dependency checks as the API; make any existing browser-audio use bounded/background-priority rather than competing with user plays. Retain safe exception stage/type/errno/HTTP status diagnostics, then retry recoverable source records through the existing album-completion lane and verify coverage, persisted details and fresh publication. Keep all-tracks coverage below six tracks, otherwise at least six and 80%, and the four-card row minimum. No application or deployment changes were made during this investigation; the memory-cookie override affected only the temporary probe process. H04 remains Fix / Validate.

Subsequent correction was accepted, tested and deployed at 2026-10-04T07:22:20Z. Production now uses extraction-owned memory cookies, worker/API token settings and dependency parity, existing worker browser-audio configuration and explicit background source verification. Safe failure diagnostics persist stage/type/errno/HTTP status without exception messages/URLs/cookies; successes clear stale failure/cooldown fields. All 95 focused checks passed. The deployed worker validated a previously failed album source in 7.34 seconds and left master cookies unchanged. Immediate post-deploy aggregate coverage remains zero qualified/25 compatible occurrences across 110 albums, with candidate work scheduled in about 14 minutes. The transport defects above are corrected; full album qualification, four-card publication, open/play and any residual provider/scheduling defects remain H04 acceptance. No coverage gate was weakened or failure record manually made verified.

By 07:29 UTC normal worker activity had recorded 66 fresh source verification timestamps after deployment. Album-source coverage reached 48 compatible occurrences and three of 110 albums passing coverage, with 47 albums lacking source records. Persisted candidate qualified_inventory is still zero pending scheduled refresh/detail completion; passing raw coverage is not itself a published detail/card. Radio reserve grew to nine cards/100 tracks, eight visible, with seven owned artist images/two valid mixes; depth/discovery gates remain open. These improvements confirm worker recovery without source resets or weakened admission. Four qualified album cards and fresh open/play remain unverified.

Read-only AWS audit compared the October 3 screenshots with main-tree Android and web-tree code, active immutable metadata, stored acquisition and existing public image bytes. It did not promote a feed or perform provider acquisition.

| Full active artifact row | Items | Nonempty cover/portrait URL |
| --- | ---: | ---: |
| Today's Pick | 8 | 8 |
| Last Played | 13 | 11 |
| Frequently Listened | 12 | 12 |
| Made for You | 9 cards | 9 |
| Because You Played | 16 | 16 |
| Recommended Artists | 48 | 4 |
| Quiet Picks | 41 | 41 |

These are full artifact counts, not necessarily the first response page. Track labels are complete, while URL presence does not establish meaningful image content. Ready depth was two. Popular Radio persisted supply had six cards/reservoir entries, four direct qualified seeds, no related/exploratory/discovery cards and three artwork repairs pending; six artwork-bearing cards did not satisfy the complete radio artwork/discovery contract. No complete radio snapshot was published. Bounded maintenance retries had delayed/no-progress work; do not spam retries or weaken contracts.

## Artwork: three distinct defects

### Provider placeholders are being treated as usable artwork

All 76 unique thumbnail URLs in the active artifact returned HTTP 200 from the server. Downloaded Last.fm PNGs were visually inspected: the white star on gray is inside the image itself, matching the screenshot. A successful image response is therefore not evidence of meaningful artwork.

Because You Played has 16 tracks with nonempty URLs, including four Last.fm placeholder images and 12 YouTube images. Today's Pick has eight candidate covers, including two of these placeholders; two of nine mix covers and one Quiet Picks cover also use the placeholder provider. Last Played has 11 URLs for 13 tracks; the two missing correspond to Chicago and Deep Purple in the screenshot.

The URL/source checks in `domain/catalog.py` accept a Last.fm URL without detecting placeholder content. `storage/artist_artwork.py` validates image encoding/MIME and size, not visual usefulness; this artist pipeline is not a shared track-placeholder repair pipeline.

Android's `preferredArtworkUrl` can derive a thumbnail from an exact valid playback source ID when the supplied URL is empty. A nonempty white-star URL bypasses that fallback on Android too. Web's `CatalogScreen._artwork` lacks this source-ID fallback and displays an icon on load failure rather than trying another valid supplied candidate. The latter is a code gap, not the proven cause of the inspected white stars.

Required direction: normalize/detect provider placeholders in the existing artwork path, recover artwork tied to the exact recording/release/source, persist repaired metadata, and reuse it across Home/Search/details. Keep network enrichment in bounded background work. Do not substitute an unrelated album cover or count an HTTP 200 as artwork success.

### Recommended Artists: missing portrait work and relevance checks

Active Recommended Artists contains 48 cards, only four with portrait URLs. The remaining 44 are missing stored portraits, rather than demonstrated browser image failures.

Web-backend `discovery/ranking.py::rank_artists` (line 1263) derives cards from artist-graph, similarity, genre/mood and popularity tracks. Unlike track-row ranking, this path does not apply the shared candidate-admission/profile-compatibility check. In the current inventory, 47 of 48 displayed artists have raw popularity-pool support; nine of the first ten have popularity support. An artist can be valid and still be a poor recommendation. These observations do not prove every unfamiliar artist is unrelated.

`home_feed/composer.py::candidate_artist_artwork_repairs` (line 133), called by the worker, schedules repairs only for inventory entries whose item type is artist. The current inventory contains zero explicit artist entries; cards derived from tracks miss this repair path. `_attach_inventory_portraits` (line 191) can attach existing exact-identity portraits, but cannot acquire absent ones.

Required direction: derive recommendations from admitted, relationship-backed inventory; use the existing durable repair jobs for exact-identity artists derived from tracks as well as explicit artist entries. Preserve genuine artist portraits and avoid fake zero-song counts, artist blocklists or album-cover-as-portrait substitutions.

### Today's Pick ignores its cover

Web `app/lib/web/catalog_screen.dart::homeRow` (line 484) hardcodes a flame icon as the leading widget. Eight persisted candidate items have cover URLs, but the card never attempts to render one.

Android `app/lib/widgets/home/neatie_home_sections.dart::NeatieHotPickCard` (line 827) displays a 78-pixel cover, separate song/artist text and styled playback control. This is a direct UI omission; improving backend artwork alone will not add the cover to web.

## Featured Albums: supply qualification and UI are separate

No Featured Albums or Recommended Albums row is present in the current active artifact. Persisted acquisition is not empty: 38 album requests produced 79 unique raw album results (33 requests resolved, five exhausted). Across their 996 canonical track occurrences, only nine have verified compatible playback sources; 115 occurrences match a source-registry entry. Forty-four albums have no matching source entries, and 35 have insufficient verified coverage. Source-state counts refer to unique keys and must not be summed as 996 distinct songs.

The existing source materialization requires all tracks for albums smaller than six, or at least six and 80% coverage for larger albums. None currently passes. The album pool excludes unplayable materializations, and the row requires at least four qualified cards. This establishes the failing boundary; it does not establish the specific upstream cause of every unavailable/no-match source. Those failures need a retained, bounded verification trace through the existing pipeline.

Keep playable detail payload and coverage requirements. Investigate verification scheduling, failed source resolution and persisted detail completion; do not force raw albums into Home or lower readiness to make a shelf appear. Historical September 29 five-album verification was a different runtime checkpoint and does not establish present AWS inventory.

Android `main_home.dart` combines featured and recommended album rows for `NeatieFeaturedAlbumHero`. The widget (line 271) uses PageView, advances every seven seconds, animates transitions and provides indicators and open/play actions. Empty album supply is hidden on Android too.

Web renders album rows as a generic horizontal ListView with static collection cards. No automatic carousel exists. The backend's current label is "Featured albums for you"; the old `featured_new_albums` key is an internal compatibility key, not proof of new releases. Use the requested visible label "Featured Albums".

## Interface comparison

| Surface | Android | Current web / gap |
| --- | --- | --- |
| Today's Pick | Cover, distinct song/artist hierarchy, styled control | Flame-only compact ListTile; ignores cover |
| Featured Albums | Responsive animated hero, indicators, seven-second advance | Static shelf if supply exists; currently no qualified albums |
| Recommended Artists | Compact circular portrait strip, wrapped names | Tall rectangular cards, icon placeholders, truncated names |
| Recent / frequent tracks | Custom horizontal strips and track actions | Generic vertical lists; rows now exist, but presentation differs |
| Mixes and radios | Distinct widgets, portrait/collage treatment | Generic collection cards; first available collage image is selected rather than a composite |
| Home navigation | All/Chill/Workout/Focus/Mood lanes; likes and assistant entry | No Home lane controls, Home likes row or assistant entry |
| Collection details | Rich entity presentation and artwork | Mix/radio collection screen has title, Play All and text-only track tiles |
| Artwork rendering | Shared AppArtwork, persistent cache, fade and source fallback | Image.network/icon handling; does not reuse Android artwork behavior |
| Search convenience | Recent queries and picked play cards | These sections are not exposed in web |

Browser HTTP caching may still apply to Image.network; the gap is the shared artwork pipeline, not a claim that web never caches.

## Home refresh: partial parity already exists

Web performs launch promotion, manual refresh, debounced listening/library signal preparation and status polling while work is pending. It displays a ready-feed banner and can populate an initially empty feed when preparation finishes. These are implemented; do not report refresh as wholly absent.

Returning to an already populated Home does not revalidate its feed unless it was awaiting preparation. Foreground visibility handling reconciles playback and refreshes Library, but not Home. Android has persistent feed monitoring and broader navigation/signal integration.

Add bounded foreground/Home-entry revalidation through the existing backend lifecycle. Preserve backend-only promotion and stable browsing: a ready artifact should not cause the client to invent readiness or replace a visible feed arbitrarily.

### Artwork audit boundaries

All 76 stored thumbnail URLs returned HTTP 200 from the server, and representative Last.fm PNGs were visually inspected. This does not prove every browser can load every URL. The album audit counted 996 track occurrences across 79 albums, not 996 distinct sources: nine verified-compatible occurrences and 115 matched registry occurrences. Forty-four albums had no matching source entries; 35 failed coverage. Among unique inspected source keys: verified 9, unavailable 78, enumerated 74, no_match 1, pending 18, unset 29, temporarily_blocked 2. The unavailable-provider causes need fresh retained traces; the coverage boundary alone is not a provider diagnosis.

## October 4 deployed artwork/artist/source correction

The observed Last.fm star URL uses provider image identity 2a96cbd8b46e442fc41c2b86b821562f. Its inspected 174px/300px PNG SHA-256 values are 2e751fc6de153bcb0070857465fdd1622aece7604496a4ebdef69b02bac5d8c9 and fad5bb8e6aadc987246977682d7b85fd841fc5ff048b955df4837189beacffcf. Local normalization rejects this URL and persisted-image validation rejects both payloads. Exact-source fallback/alternate candidates are persisted; the web renderer tries alternatives on image-load failure. Existing richer artwork is retained through thin updates.

Recommended Artists, reserve accounting and track-derived portrait repair now share candidate admission. Portrait repair projects only artist identity/artwork evidence; it excludes recording IDs/covers and matches nested credits by artist name. Homonymous stable IDs remain separate. No artist/title/language blocklists were added.

An explicitly approved aggregate AWS read found 500 unavailable source records with generic stream_failed errors and 112 temporarily_blocked/source_blocked records, across the entire stored track-source registry rather than the 79-album subset above. The code treated every failure except source_blocked as permanently unavailable. Generic transport and format failures now cool down and retry through the existing bounded verifier, including legacy persisted records; deleted/unavailable videos remain terminal. Album coverage and persisted playable-detail requirements are unchanged. Failure codes alone do not establish the underlying provider/network cause or prove live recovery. Deployment is verified; bounded source recovery and real published album/portrait checks remain pending.

## October 4 album/radio findings and deployed corrections (H05–H08)

- Exhausted album seeds were filtered after the 16-artist bound; the same exhausted frontier could hide later eligible artists indefinitely. A 32-item priority list also crowded supported seeds before the candidate cap. Local correction filters terminal provider requests first and rotates supported seeds, preserving independent healthy providers. Inventory used truthiness for next cursors, so wrapping to zero retained the previous nonzero cursor; this is corrected and seed-progress diagnostics persist.
- The radio compact index associated a cached verified stream with a canonical row without rematching its title/artist/version and retained only the last retrieved source. Local correction reuses the existing semantic matcher and considers all verified candidates; wrong live/studio matches cannot acquire playback through this join.
- Radio discovery seed projection read raw pools before shared admission. Local correction uses admitted artist credits and excludes suppressed/unrelated candidates, without copying recording IDs or covers into artist evidence.
- Inventory readiness counted exploratory mixes as discovery while publication requires three non-mix discovery cards. Mix image checks counted duplicate paths and did not bind each image to its credited identity; artist thumbnail paths could disagree with declared owner tokens. Local readiness/selection/publication checks now enforce the existing contracts.
- Novelty diagnostics counted a new synthetic mix ID as a new artist and used a four-card diagnostic threshold instead of the existing three-artist contract. They now compare actual credited artist identities. Invalid reservoir entries no longer inflate qualified discovery/unexposed counts.

288 focused backend checks pass, including three complete rotations with new-first ordering and persisted source/metadata/artwork regressions. H05–H08 are uncommitted and deployed as 2026-10-04-album-radio. Release/HTTPS/hash/health and external Chromium HTTP/1.1 navigation checks pass. The user restored SSH access by updating its My IP rule and authorized non-sensitive database audits; sensitive access still requires permission.

Post-deploy aggregate audit: two ready successors; old active/prepared payloads still carry placeholder URLs and limited portraits. Album acquisition grew to 46 requests (38 resolved, eight exhausted) and 92 stored results, with zero qualified albums. Radio has six cards/72 tracks, complete metadata, no duplicate physical sources, four identity-owned artist thumbnails and two valid mix-image sets; it fails minimum card count/non-mix discovery supply. The final newest-inventory audit returned the same supply counts. Across the full track-source registry, 1,041 generic stream_failed and 104 source_blocked records were retry-eligible; 82/one respectively were not yet eligible. These counts change as cooldowns expire; eligibility is not verification success or a provider diagnosis. No post-deploy artifacts/inventories had been generated at the freshness audit; existing radio/candidate jobs were due about 16/26 minutes later. Normal backoff remains; fresh enrichment and provider recovery are not claimed.

## Search and metadata defects: historical cause, deployed correction

September 30 snapshots of `dont stop me now` returned Queen first in Songs but null Top/lead and 23 mixed-artist artist_tracks. The target competition was album score 7.5 versus track 6.9788, margin 0.5212, mixed/ambiguous confidence zero, competing_authoritative_entities/ranked_target_missing. `im still standing` had Elton John Top but Songs started with TECHNO N TEQUILLA. Warm snapshot load took 2 ms, response construction 630–669 ms and request 634–672 ms while old diagnostics.total_ms claimed 4 ms. These are historical samples, not current benchmarks.

Sufficient old code causes were no-lead artist expansion admitting every candidate; append-only snapshot/client merging unable to retract contaminated rows or null targets; independently ranked Top/Songs; and interrupted Search timers not resuming on re-entry. October 1 fixes were deployed October 3: require accepted lead/identity, prune merged snapshots, mark/reconcile accepted playable targets, apply authoritative first-page revisions while preserving separately fetched pages, fence old generations, resume polling and use consistent timing. These fixes are not still waiting for deployment. Scope/alias/credit/version and genuine ambiguity regressions must remain protected.

September 30 authorized metadata inspection of Dio + Unknown Artist Mix found seven of 12 tracks Unknown and physical source `347vCib_lMs` also linked to Purple Rain/Prince. Another Dio source had conflicting canonical metadata. Contamination predates Flutter rendering. Physical-ID-first merges and compact-index validation were fixed, but newly built radio acceptance and complete independent supply remain H06–H08.

The October 3 Everlong disappearance was a separate same-revision timeout response carrying empty surfaces. Backend now acknowledges not_modified without replacement surfaces; web retains results. Public Chromium passed repeated timeout/full-revision handling; iPhone acceptance remains S05.

Still open: cold first-query `when september ends` took 54.13 s on AWS, Amel Music ranked before Green Day; prior screenshot had Infinite Paradox 369 before Green Day. A selected-source personalized repeat or passing fixture does not establish first-attempt success. Keep S01/S02/S06 for target relevance, stale-serving product behavior, cold/warm measurement and response construction, using one Search pipeline.

## Playback hypotheses and diagnostic requirements (P01–P06)

### Bluetooth and lock-screen resume

User reports immediate paused resume works, delayed resume and manual lock-screen Play fail, while Pause works. Current `browser_audio.dart:185–210` registers Media Session handlers and publishes paused/playing state. Ordinary pause does not dispose the element, clear its source, or unregister handlers; disposal is a separate path. Resume calls the existing HTML audio element directly. There is no application pause-expiry timer found.

Browser/OS media-session suspension is a plausible cause, not confirmed. Historical [WebKit bug 243258](https://bugs.webkit.org/show_bug.cgi?id=243258) reports failure after a five-second background pause on iOS 15.6 PWA; it is analogous evidence, not proof about this Brave/iOS version.

Next diagnostic split: (a) no `media-action:play` means command delivery/session ownership failed; (b) action received but no accepted play requires the Promise rejection and media error; (c) accepted play without advancing time requires buffer/stall evidence. Record pause duration, visibility, session/operation, build, iOS/Brave version and audio route. Compare immediate, 5-, 30- and 120-second pauses, Bluetooth and manual controls, then foreground resume. Preserve real paused semantics; do not fake continuous playback or promise background execution with timers.

### Lock/leave during loading

The narrowly classified startup interruption is MEDIA_ERR_ABORTED (media code 1); preserve its distinction from genuine network/decode/unsupported failures.

The September 29 handler initially handled only explicit media abort (code 1) while loading after backgrounding: retains source and offers same-source resume (`web_player.dart:429`). Network/decode/unsupported errors still invalidate and report failure. October 1 extended explicit-abort recovery beyond Promise settlement; this remains implemented recovery, not proven prevention of the reported failure. Code paths distinguish play-Promise rejection from media-element error; a device event sequence is still required to identify which occurred.

Previously supplied September 27 log ends at the next track's play-call. Earlier preparation failure belongs to another requested track while the current track continues playing; it cannot establish the startup failure cause. Test warm and cold starts, lock before first playing, app-switch before first playing, and lock after playing. Capture retained diagnostics after returning, including mediaCode/message and play rejection. Correlate source ID and timestamps with server first-byte/range/prepare logs. Preserve genuine network failures rather than treating every error as an interruption.

### Repeated discovery queues

`web_player.dart:72` restores saved queue/session/history. `start()` at :221 creates a new discovery session; `startQueue()` preserves explicit collection order and disables discovery replenishment. `music_api.dart:202` sends session/recent IDs. `media_runtime.py:29` caches raw provider related results for 300 seconds; exclusions and session ordering are applied afterward (:282), so this cache does not itself freeze personalized order.

Confirmed limitation: session hashing only reorders existing candidates within 12-item relevance bands. A small/repetitive pool cannot become diverse through shuffling. Reload deliberately restores the same active queue. Next work: measure candidate count, exclusion count and next-10 overlap across new starts; refresh/expand supply through the existing related pipeline when exhausted; provide an explicit new discovery/refresh action without silently rewriting the active queue or an album/playlist. Separate discovery freshness from audio-cache availability; deleting R2 audio does not improve recommendations.

### Other retained evidence limits

- Under Pressure September 25 15:38 Vietnam / 08:38 UTC API logs were lost in the September 26 container replacement. Later transfers cannot reconstruct the failure.
- Earlier September 18 excerpt did not contain The Kill failure. September 27 trace ended at the next track play-call and included failed preparation for a different track from healthy audio.
- R2 fingerprints matched Android/Oracle; the sampled 505 source `qU9mHegkTc4` returned HEAD 404. Exact Android source/local cache state is unknown. Do not infer duplicate remote caches or identical bytes from matching titles.
- Earlier iOS NotAllowedError demonstrated lost gesture permission after asynchronous resolution in that session; it does not explain all later failures.
- Hellraiser recorded 34.9 s preparation, zero queue wait, 4.9 s first-MB delivery; a different request waited 16.8 s in the scheduler. Separate these stages.
- Byte-based capture completion and media validation were implemented after fully transferred captures waited for playback end. Prior actual captures passed and truncated copies failed; WebM warnings alone did not prove data loss.
- Browser source/session/UI/Media Session must agree. User explicitly reported one preview and no other music app; do not dismiss mismatch as another app without evidence.
- Prepared metadata, source URL/prefix and fully validated cached bytes are distinct. Sequential first-five/next-five warming is bounded and yields to actual playback. No blanket sub-three-second cold SLA.

## Android account ownership (A05/A06)

The September 7 cleanup audit identified two races; October 3 inspection still matches the source in both trees:

- `app/lib/logic/library_catalog_provider.dart` loads likes using activeStorageScopeId, awaits, then obtains that global scope again for the write. Cloud mutation obtains currentAuthenticatedUserId after local persistence. Capture the initiating account and fence the entire operation. Flutter account-switch reproduction is pending.
- `app/lib/logic/recommendation_preferences_provider.dart` retains an auth-independent notifier. Load/save apply payloads without checking initiating scope and no auth-dependent recreation is present. Old settings may remain visible or be saved under the new scope. Preserve the feature; reload/reset and fence responses.

These are separate from web scope checks and playlist preflight conflicts. WEB_PLAYLIST_WRITES stays false until atomicity, partial failures and Android/two-device rollout pass.

## Tests and cleanup: retained September 7 partial audit (T01–T08)

The old semantic audit covered selected implementations/dependencies/tests, not all 63,615 net added lines. Its inventory was backend production 35,657; Flutter/config 8,824; native 11; tests 16,417; docs/tooling 2,706. Tests were 25.8% of net growth; two Search suites contained 8,758 physical lines/167 methods, all backend tests 688,252 bytes. These are historical scale measurements, not current totals or a promised reduction target.

1. **Readiness fixture:** test_catalog_acceptance.py:75 expected production readiness from one canonical track with no playable sources. Historical 403 passed / 1 failed / 4 skipped run reproduced the failure; September 26 also reproduced it on pristine HEAD. Correct the expectation/scope, not production readiness. Recheck latest fixture before editing.
2. **Overstated acceptance:** test_home_feed_build_budget_v2.py:1301 mocked both row building and quality; it tested publication/storage/structure rather than real speed/relevance. test_new_releases_service.py:66 accepted detail_ready without opening a persisted playable detail. Keep structural coverage and add real contract checks.
3. **Unused Search entry points:** resolve_canonical_tracks, run_external_catalog_import, enqueue_external_catalog_seeds, schedule_catalog_population and catalog_playable_tracks_for_query had no production callers in that audit; October 3 checks still find the named definitions. Eleven old tests/~538 lines use these paths. Recheck dependencies, preserve unique alias/identity/persistence assertions in the active path before deleting anything. canonical.py/catalog_pipeline.py also contain active shared helpers and exposed coverage reporting.
4. **Orphan candidates:** historical review proposed 327 canonical.py lines, 711 catalog_pipeline.py, six Home helpers/~444 and Flutter methods/~140: roughly 1,600 production lines for review, not guaranteed removals. The intent-delta writer remains referenced while its old consumer was removed; retire only proven unused storage, preserving events/scheduling.
5. **Fixtures:** repeated provider/database/cache/artwork setup can be shared. Historical examples test_search_latency.py:2532/:3004 and test_search_enrichment.py:3607/:3651 protect different cases; similarity is not redundancy.
6. **CI:** inspected workflows lack backend pytest; Android release runs only app_update_provider_test.dart. Add retained deterministic suites after fixing test scope, keep live-provider probes opt-in and device checks separate.
7. **Assets/packaging:** old audit found branding neatie_app_icon.png, neatie_intro_icon.png and neatie_intro_mark.png unreferenced/absent from explicit assets, each 414,242 bytes (1,242,726 combined). Recheck design/runtime references before deletion. Proxy Docker/archive packaging copied broad context; exclude only verified development-only files, not necessary runtime imports.
8. **Documentation/native:** current Gradle uses CMake and managedJniLibs. Old transfer-only jniLibs guidance and travel/work/Oracle-only instructions were stale. This consolidation fixes current guides; historical native/device and actual workflow configuration gates remain.
9. **Contract review:** New Releases hydration now reads actual detail tracks, but track IDs/detail_ready alone do not establish every source's playability. Popular Radio base-contract and novelty warnings need acceptance review; do not remove guards as cleanup. Android New Releases baseline was accepted; reopen only a fresh defect.
10. **Recorded full Flutter failures:** personal_mix_detail_screen_test, recommendation_feed_models_test and track_menu_button_test were the three non-web failures with 82 passed September 26. No full-suite green or baseline claim was established for all three. Recheck, do not delete failing tests to claim success.

Keep real SQLite/immutable-write/lease/restart/exposure tests, active Search identity/artwork guards, source/version matching and fresh-versus-usable semantics. A prior intentional removal of version rejection made the studio/live regression fail, demonstrating protection; the function was restored automatically. Cleanup is optional after behavior, not a new architecture or blanket line-reduction exercise.

## Remaining acceptance and guardrails

- Canonical identity alone proves neither playback, album/artist detail readiness nor meaningful artwork.
- Backend alone promotes artifacts; Flutter validates scope/version and offers offline recovery. No client quality gate, duplicate lifecycle or fabricated success state.
- Feed composition consumes persisted inventory; providers and artwork work stay bounded/background. Popular Radio remains independent.
- Genuine account events remain evidence; recommendations do not become new history. Scope all async/storage work.
- Maintain one canonical Search and source/stream pipeline; preserve uncertain valid names and explicit version intent.
- Native symbols/launch performance and signed self-update need device tests. Charts provider/range/region, full filter lists, meaningful NEW markers and mini-player navigation retain gates.
- Lyrics are provider bounded; Behind the Lyrics interpretation/error handling remains separate. Ask Neatie re-audit was explicitly deferred.
- No secrets, cookie bytes, raw private runtime, copyrighted lyric dumps or migration archives belong in docs/Git.
