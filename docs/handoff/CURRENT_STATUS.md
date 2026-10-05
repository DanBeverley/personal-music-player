# Current Status

Reconciled October 5, 2026. This is the deployment/verification record; [NEXT_STEPS.md](NEXT_STEPS.md) is the complete queue, [KNOWN_ISSUES.md](KNOWN_ISSUES.md) owns diagnostic evidence, and [OPERATIONS.md](OPERATIONS.md) owns hosting/backup/rollback details. Dated reports were consolidated into these six handoff documents; historical checkpoints below retain their original scope.

## Source and serving checkpoint

| Purpose | Directory | Branch / application checkpoint |
| --- | --- | --- |
| Android/main | C:/Users/ADMIN/Documents/GitHub/personal-music-player | develop / e720709 |
| Web/backend source | C:/Users/ADMIN/Documents/GitHub/personal-music-player-web | codex/web-app / e4e7529; application changes committed, tests/runtime/private excluded |

The worktrees differ. Main-only playlist delta-sync changes are not released and are not assumed integrated into web. No reset/clean, unrelated revert, commit or push is implied by documentation work.

- Serving build: `2026-10-05-editorial`.
- Web: https://preview.13.236.9.131.sslip.io/
- API: https://13-236-9-131.sslip.io/
- Public main.dart.js SHA-256: `580d9058a3bd68bb80ec0b5614ccf2f6948e2ef75a20149ad44187acafc1a893`.
- Host: user-created AWS Sydney EC2, m7i-flex.large, Ubuntu 24.04.4 x86_64, 2 vCPUs / 8 GiB / approximately 40 GiB gp3. Oracle is stopped; no A1 has been allocated.
- September 20 runtime restored on AWS; both SQLite databases passed integrity checks. This does not recover later Oracle runtime activity.
- All six services, HTTPS/public bundle, history gateway routes, actual desktop Chromium playback/seek/reload and API/worker restart persistence were verified. Stable IP association is unverified.
- The downloaded October 3 AWS archive is hash/SQLite verified and predates the latest history/Search changes. Its full restore is untested; on-host daily backup timer is active. Paths and exclusions are in OPERATIONS.

## Implemented behavior

### October 5 quiet minimal frontend

- AWS promotion completed 2026-10-05T01:52:48Z as 2026-10-05-editorial. All 56 source/assets/bundle manifest hashes and the public HTTPS bundle hash matched. Only web-preview was recreated; all six services run and API/browser-audio/token services remain healthy. The exact previous static bundle, frontend source backup, online runtime backup and guarded rollback script are retained in OPERATIONS. No rollback was required.
- Public Chromium over HTTP/1.1 verified the exact bundle/build/API origin, default Home, Search and configured email/Google account controls at desktop width, then the 390px account layout. One audio element and zero page errors were observed. This public check is unauthenticated navigation; real-account feed content and real iPhone playback remain unaccepted.

- User preference: quiet minimal, desktop first. The redesign adds warm typography, soft sage accents, generous spacing, a desktop sidebar, larger exact daily-pick artwork, an album spotlight, lighter song strips and circular artist presentation. Search, Library, account and catalog detail share the same visual system. Home is the initial destination. Fonts are locally bundled with OFL licenses.
- Desktop uses a compact horizontal playback dock; phones use a compact dock with an expandable artwork/player sheet. Both control the existing BrowserAudio element. Play/pause, seek, previous/next, repeat, likes, queue, loading and interruption messages remain available. Account scopes, backend promotions, provider data, source verification and playlist mutation gates retain their contracts.
- Source commits: 6b0a1e8 (Home admission/artwork), bf8d4ee (album/radio supply), acd8117 (album transport), f94103d (H09–H14 presentation), 2b1f5f4 (visual foundations/fonts), e4e7529 (responsive screens/player). Each commit has a scoped body. Tests, runtime, generated bundles and private files were excluded; nothing was pushed. Pre-redesign source tag: neatie-web-before-editorial-20261005 at f94103d.
- Verification: 68 focused Flutter Home/catalog/artwork/history/player checks passed; release compilation and WebAssembly dry-run passed with the existing Cupertino font warning. Synthetic Chromium at 1440px, 390px and 320px passed default Home, carousel selection/detail return, Search/Library, dock/expanded player, repeat and queue actions, one audio element and zero page/console errors. Separate native canvas pointer checks passed play/pause/seek; keyboard checks cover the semantics-enabled controls. Real iPhone, account and supply acceptance remain open. Temporary visual evidence: C:/Users/ADMIN/AppData/Local/Temp/neatie-editorial-20261005/.

### October 4 album transport correction and deployment

- Latest authorized audit: 110 raw albums, zero qualified; 25 of 1,469 album-track occurrences have fresh compatible verified sources, and 49 albums have no source records. Acquisition progresses, but verification remains the failing boundary.
- Bounded, user-authorized live probes reproduced yt-dlp cookie-save OSError on the read-only cookie mount under both API and worker extraction settings. A diagnostic-only in-memory cookie loader removed that error; worker settings then hit source_blocked while API client/token settings validated audio. The existing browser-audio route also validated audio. Worker runtime lacks the API's token-provider/browser-audio settings; node defaults and binary presence are valid.
- The subsequent accepted correction is deployed: yt-dlp loads a fresh extraction-owned memory jar without cookiefile save-back; API and worker share existing client/token settings and healthy dependency checks. Worker has the existing browser-audio route, and source verification explicitly uses background=True, preserving its existing bounded priority. Failure records retain only stage/type/errno/HTTP status diagnostics; successful verification clears stale failure/cooldown fields.
- All 95 focused backend checks passed, covering actual cookie loading/no save-back, extraction-error preservation, concurrent jar isolation, safe diagnostics, background verification, retry/format/circuit guards and persisted album detail. Only two backend modules and two compose files were uploaded; tests/docs/private files were excluded. API/worker were rebuilt and recreated with all four overlays, online SQLite backup and source/config/image rollback. All six services run; API/browser/token services are healthy, merged configuration parity and four file hashes passed, and the master cookie contents are unchanged. No commit/push occurred.
- Deployment completed 2026-10-04T07:22:20Z. A bounded direct extraction/media preflight of a previously failed album source inside the deployed worker succeeded in 7.34 seconds, with the master cookies unchanged. Web build remains 2026-10-04-home-presentation; no client bundle was rebuilt. Immediate aggregate audit still found zero qualified albums and 25 verified-compatible occurrences across 110 albums; normal candidate work was due in about 14 minutes, radio in two. H04's production transport correction is complete, but fresh worker supply, four-card publication and album open/play acceptance remain pending. One source recovery does not close album acceptance. Root-cause/recovery evidence remains in KNOWN_ISSUES; operational checkpoint is in OPERATIONS.
- Later normal-worker audit by 07:29 UTC: 66 source records have new verified_at proofs after this deployment; no retained failure-stage records in that update set. Raw album coverage improved to 48 compatible track occurrences and three of 110 albums passing the coverage gate (47 without source records); these are source-level counts, not persisted qualified cards. Candidate qualified_inventory remains zero pending its scheduled refresh and detail completion. Radio reserve is now nine cards/100 tracks, eight visible, seven owned artist images and two valid mixes; minimum-card/discovery contract still fails. Ready depth is two. Worker progress is demonstrated; full album/radio publication acceptance remains open.

### October 4 H09–H14 continuation and deployment

- H09: additional regression verifies guest/account history counts through repeat, serialized reload, restored-position suppression and repeated Home signal reads. Existing progression/seek/pause/source/epoch guards remain; real-device and cloud merge acceptance are pending.
- H10: Home/tab/visibility return issues a queue-status request with zero long-poll wait, coalesced with an existing in-flight check and throttled for 30 seconds. Populated rows, detail navigation and scroll remain stable; backend readiness can expose a successor, but only explicit refresh promotes it. Hidden/inactive Home stops status polling.
- H11/H12: Today's Pick attempts its exact cover and uses distinct song/artist/play presentation. Qualified album rows form one responsive Featured Albums hero, with seven-second/650ms advance, manual indicators, open/play, touch pause, inactive pause and reduced-motion support. Embedded tracks play within the gesture; otherwise Play opens persisted detail for another explicit play gesture.
- H13/H14: circular artists, distinct mix/radio cards and real multi-image collages; recent/frequent and scoped-like strips; ordered track actions and collection details with cover, metadata, Play all and selected-index playback. Lanes consume existing backend diagnostics and hide unsupported lanes. Existing row paging remains. Playlist mutation gates and deferred assistant scope remain unchanged.
- Source lives only in the web worktree. The user authorized deployment; AWS now serves `2026-10-04-home-presentation`. The six web sources and bundle were verified with seven manifest hashes, the previous web/source release and an online runtime backup were retained, and only web-preview was recreated. API/worker were not rebuilt or restarted. All six services run; API/browser-audio/token-provider are healthy. Public HTTPS, exact bundle/hash/build/origin checks passed. No commit or push occurred. Device acceptance remains in NEXT_STEPS.
- Verification: 81 focused Flutter Home/catalog/artwork/history/player checks passed; release compilation and WebAssembly dry-run passed. The build retains the existing Cupertino font warning. Local Chromium with synthetic feed/artwork fixtures passed desktop/390px rendering, manual album selection/detail navigation, page/indicator agreement after detail return, one audio element and zero page errors. Visual inspection covered hero/pick, mix/radio collages and circular artists. The check exposed and fixed shared horizontal scroll-storage collisions and stale hero-page restoration; regressions cover independent row offsets. Fixtures do not establish fresh AWS supply or real iPhone playback. Temporary visual evidence: C:/Users/ADMIN/AppData/Local/Temp/neatie-home-presentation/.
- Fresh authorized AWS audit around 06:42–06:46 UTC: candidate/radio inventories updated after H05–H08, with album seed diagnostics present. Album requests increased to 49 (41 resolved/eight exhausted), with 98 stored raw albums and zero qualified inventory. Across 1,250 canonical track occurrences, 226 have stored source records and only 23 have fresh semantically compatible verified sources; 42 albums have no source records and all 98 fail verified coverage. Relevant source records include 150 stream_failed and two source_blocked failures. This locates the source-verification/coverage boundary, not the underlying cause of every stream failure. H04/H05 album acceptance remains unresolved; the carousel cannot create qualified supply.
- Radio reserve increased to seven cards/80 tracks, with six visible, complete metadata, no duplicate physical-source cards, five owned artist images and two valid mix-image sets. Publication still fails minimum-card and non-mix-discovery requirements. Existing active/ready artifacts remain older; zero post-H05–H08 artifacts for the audited listening scope, ready depth two. Worker backoff was retained; candidate/radio work was due in about 10/13 minutes at audit time.
- External public Chromium over HTTP/1.1 loaded the new build, configured Supabase sign-in and Home at desktop/390px widths, with one audio element and zero page errors. This verifies serving/navigation, not account-specific feed content or real iPhone playback.

### October 4 H01–H04 deployment

- Existing source was committed in six batches: c9485b0 (streaming), b23ed18 (catalog/history), 099e26c (Search), e824a35 (Home), 6e3b853 (web client), a55b5d9 (deployment configuration). Commit bodies describe scope. Tests, documentation, private configuration and generated output were excluded; nothing was pushed.
- H01: reject the observed Last.fm star URL and both inspected PNG byte fingerprints; validate legacy stored image bytes; persist exact-source cover alternatives; Home/Search/details try alternate images on failure. Existing richer covers survive thin metadata updates.
- H02/H03: Recommended Artists and reserve coverage use shared admission; identity-owned portrait repair includes admitted track-derived credits. Different artist IDs cannot share artwork merely through a matching name; recording covers/IDs are excluded from portrait repair inputs.
- H04: an authorized read returned 500 source records marked unavailable with generic stream_failed errors and 112 temporarily blocked source_blocked records. Recoverable failures now have bounded retry cooldowns, including legacy records; genuinely unavailable videos remain excluded. Saved candidates are semantically rematched before verification. Album coverage and persisted detail requirements remain unchanged.
- Pre-deployment verification: 275 focused backend tests, 25 Flutter tests, release web compilation and WebAssembly dry-run passed. Both observed HTTP-200 placeholder payloads were rejected. The final cooldown/cover-priority adjustment passed 37 source-registry/album checks. AWS API/worker were rebuilt and the web gateway recreated with all four existing overlays and private mounts retained. All six services run; API/browser-audio/token-provider are healthy. Public HTTPS, exact bundle hash/build/origin and deployed placeholder/retry helpers passed. The new Flutter shell and configured sign-in UI loaded in external Chromium over HTTP/1.1; default browser transport initially timed out during bundle loading. Fresh artifact/portrait enrichment, qualified album open/play and real-device acceptance remain pending. Existing immutable feeds are not rewritten.
- External Chromium over HTTP/1.1 also opened Home at desktop and 390px widths with one audio element and zero page errors. This shell/navigation smoke does not establish fresh feed content, actual playback or iPhone acceptance.
- Rollback source/web, API/worker images and an online SQLite runtime backup were retained under the October 4 checkpoint in OPERATIONS. The targeted release was packaged before H05–H08 edits.

### October 4 H05–H08 deployment

- H05: rotate the supported album seed frontier; exclude exhausted provider requests before the 16-artist bound, including legacy progress keys; preserve healthy providers for the same artist. Cursor wrap to zero now persists, and aggregate seed-progress diagnostics survive inventory serialization. Existing listening evidence and album coverage remain unchanged.
- H06: retained radio acquisition rows must pass the existing title/artist/version source matcher before receiving cached playback facts. All verified source candidates are considered instead of retaining whichever SQLite row was read last.
- H07: radio discovery acquisition consumes admitted artist credits through the existing shared policy; raw unrelated popularity and suppressed candidates cannot enter through this frontier. Readiness uses the existing publication contract, including 10–15 cards and three non-mix discovery cards.
- H08: artist thumbnail paths must match their identity tokens; mixes need at least two distinct images owned by credited source identities. Invalid reservoir cards cannot inflate discovery reserve counts. Novelty diagnostics count actual artist identities, not changed synthetic mix IDs, with the existing three-artist threshold.
- Verification: 288 focused backend tests passed, including exhausted/legacy/profile-frontier and cursor persistence, wrong live/studio source joins, artwork ownership, metadata/source deduplication and three complete radio rotations with new-first ordering. Release compilation and WebAssembly dry-run passed. H01–H08 remain uncommitted; tests/docs/private files remain uncommitted, and nothing was pushed.
- The user updated the SSH My IP rule after timeouts. H05–H08 were deployed with an online SQLite backup, source/web/image rollback and eight checked source/bundle hashes. All six services run; API/browser-audio/token-provider are healthy. Public HTTPS, exact bundle/build/origin checks passed. External Chromium over HTTP/1.1 loaded the shell, configured sign-in and Home at desktop/390px widths with one audio element and zero page errors. Deployed constant-input album-frontier and radio-artwork guards passed. This does not establish iPhone playback or fresh supply acceptance.
- The approved read-only aggregate audit found seven existing Home rows and two prepared successors, with complete track metadata but older stored placeholder URLs/limited portraits. Album acquisition has 46 requests (38 resolved, eight exhausted), 92 stored results and zero qualified inventory. Radio has six cards/72 tracks, zero incomplete labels or duplicate physical-source cards, four owned artist images and two valid mix-image sets. It fails minimum card count and non-mix discovery supply. There are no post-deploy account artifacts or refreshed inventories yet; pending radio/candidate jobs were due in about 16/26 minutes at the audit. Backoff was retained, not forced. Fresh enrichment, source recovery, published album/radio open/play and three live rotations remain open.
- User authorized non-sensitive database audits going forward; ask before sensitive access. Rollback/backup paths and release hashes are in OPERATIONS.

| Area | Implemented / deployed | Remaining limit |
| --- | --- | --- |
| Android baseline | Durable Home, canonical Search, source-aware playback, discovery queues, library, Charts/Releases, lyrics, recognition/assistant, update infrastructure | Related-song queues, Search scrolling and New Releases accepted by user; retained device/contracts in the queue |
| Web shell/player | Responsive PC/iPhone navigation, one browser audio source, local account-scoped queue/position/duration restore, Media Session | Not full Android UI parity or cloud handoff |
| Audio delivery | Gesture-safe start, prepared-source reuse, foreground priority, bounded same-source range/capture retries, byte-based capture completion, remux/decode/duration cache validation, sequential first-five/next-five warming | Upstream access/cold latency and iPhone suspension remain provider/device bounded |
| Diagnostics | Selected/loaded source and operation fences, foreground reconciliation, persistent scoped failures, compact queue/prepare/transfer timing | Lost old logs cannot be recreated; newest device traces still needed |
| Search | One canonical pipeline, corrections/original-query opt-out, alias/credit/version guards, authoritative revisions, page retention, resumed polling, private explicit-selection memory | Cold first-query relevance, stale-serving product behavior and account/device acceptance remain open |
| Home | Typed rows/details, actual-play history/counts, guest/account scopes, debounced signals/preparation, backend launch/manual promotion, immutable snapshots/bundles | Artwork, qualified optional album/radio supply, relevant artists and UI remain incomplete |
| Auth/library | Email/password, Google, signup/reset/recovery, session persistence, cloud likes and existing ordered playlists | Ordinary user login observed; full recovery/Google/isolation/parity matrix pending |
| Playlist mutation | Targeted web repository scaffolding and main Android delta-sync/helper regression | WEB_PLAYLIST_WRITES=false; atomic concurrent edits, offline/partial failures, controls and two-device rollout remain gates |

### October 3 deployed history/Search changes

The earlier empty signed-in Home and Unknown labels arose from bare restored Android events, incomplete provider detail skeletons and persisted placeholder snapshots. Delayed initial candidate work retained stale hints; a premature shelf diversity cap discarded multi-card mix supply. A trusted-history admission bypass and old bundles retained placeholders. Search timeout polls returned empty surfaces and web treated them as replacement pages.


- History snapshot model v2 recovers metadata using exact persisted catalog/entity-source matches, preserves listening IDs/events/counts, and prevents placeholders overwriting known metadata. Normal history response construction does not call providers. Missing metadata repair uses the existing bounded background path and validates remote details before caching.
- `get_watch_playlist` detail recovery uses the queue entry matching the requested video ID when video details are incomplete. Invalid cached/get-song skeletons are ignored. Local guest history rejects incomplete incoming/restored display snapshots.
- Initial Home Retry replaces delayed stale candidate work with current signals, without duplicating leased work or bypassing provider backoff. Unknown placeholder artist hints are excluded from acquisition planning.
- Mix ranking retains the admitted multi-card reservoir; each card enforces artist/album diversity. The first eight tracks remain unique across cards; deep overlap stays bounded. Home admission and row minimums are unchanged except that trusted history must also have usable title/artist metadata.
- Shared admission rejects missing/placeholder track display metadata before the personal-history/non-discovery bypass. Bundle schema v2 makes older immutable bundles recompose under this rule; original history and artifact records are retained.
- Search timeout acknowledges `not_modified` and omits replacement surfaces/pagination. Web retains results on that acknowledgement; actual newer revisions remain authoritative, including removals and null clearing.

### October 1 batch, deployed October 3

- Search v10: optional-apostrophe/phrase relevance, stable lead-artist/alias identity, whole-snapshot contamination pruning, accepted Top first in Songs, authoritative revisions/null clearing with paged data retained, old-response fences and polling after tab/detail return.
- Response timing boundaries were unified; exact history suggestions avoid cold catalog scans and overlay only shared nonpersonal metadata. Account rows are not shared.
- Radio repairs reconcile case-sensitive physical IDs before canonical identity and preserve complete title/artist metadata through verification/compact indexing. Insufficient supply is not padded and old artifacts are not rewritten.
- Explicit media abort recovery retains/reloads the same URL even after the start Promise settles; stale-operation/source fences remain. Genuine network/decode/unsupported failures stay failures; DOM/media diagnostics are bounded and URLs redacted.
- Plays/counts require actual progression; repeats need fresh progression, while seek/pause/restored positions do not count. Signed-in history uses history_seed; scope/instance fences and local/server reconciliation avoid duplicate counting or guest import.
- Home debounces changed signals 750 ms, coalesces identical inputs for 45 s, and reconciles fresh supply in the existing worker at most once per five minutes. Stable launch tokens allow one backend launch promotion; background preparation preserves visible rows.
- Missing primary artwork permits other supplied portrait/collage fallbacks; unknown artist work counts no longer display zero. This does not solve placeholder-image content or portraits missing from supply.
- Gateway history_seed/interaction_event additions were deployed with backend/web. All source fixes above still require the acceptance gates in NEXT_STEPS.

## Verification and acceptance

### Latest history/Search verification


- 140 Home/history/ranking/fixture/worker/repository/lifecycle regressions pass. Another 50 source-registry/related-track/history-eligibility/album-detail/Search-routing checks pass. Earlier overlapping backend suites are not summed with these.
- 40 focused Flutter catalog/API/guest-history/Home-signal tests pass; release web build succeeds. Flutter analyze remains a user-run check. Existing Cupertino font warning remains.
- External Chromium verified the public bundle and retained Everlong across timeout polls and a later full revision, with no page errors. The test used real public API responses, not mocked results.
- Affected account history returns 9/9 recent and 12/12 frequent entries with usable titles/artists. Server verification emits counts/flags only, without account IDs or private track names.
- Final affected-account Home response is `servable` with seven rows: Today's Pick 8, Listening History 13, Frequently Listened 12, Made for You 9 cards, Because You Played 12, Recommended Artists 10 and Quiet Picks 20. All track titles/artists are complete, including 101 entries in nine mixes (card depths 17/10/10/16/10/10/9/9/10). Artwork URLs exist for 11/13 history entries and 99/101 mix entries; this does not prove every URL fetches. Optional album/radio shelves remain absent in this verified feed. Portrait, radio/album and device acceptance remain separate.
- Ordinary serving (without force-refresh) retains this active feed and reports two complete prepared successors. All six deployed services are running; API, browser-audio and token-provider health checks pass.

### Other recorded checks — overlapping suites, not cumulative

| Checkpoint | Recorded evidence | Limits |
| --- | --- | --- |
| October 3 AWS deployment | 17 focused backend tests; new web build; real external Chromium Green Day decode/progression, seek, pause/resume, navigation, queue reload; HTTP 206 65,536-byte R2 range in 0.61 s after restart | First playback test's 10.48 s includes five required progression seconds, not first-byte latency; not iPhone or full account acceptance |
| October 1 implementation | 349 focused backend / 65 Flutter tests; web release build; generated-audio Chromium repeat, two transitions, seek, scoped play events, navigation, reconcile, reload and narrow viewport | Fixtures/local checks did not establish live first-query ranking or device correctness |
| September 29–30 | 56 focused web; 65 Home/album backend; 34 worker/history; final 37 artwork/album/history and 28 source-registry checks | Suites overlap; old Oracle runtime outcomes do not establish current AWS inventory |
| September 28 | 51 web before final placement correction, then 14 catalog screen tests; 138 backend, then 61 Home after portrait reuse | Overlapping targeted suites, not full-project green |
| September 26 | 52 focused web / 34 backend final; earlier 84 playback/cache/correction and 122 ranking/Search checks | Full Flutter recorded 82 passed / 3 non-web failures; one catalog test also fails on pristine HEAD |
| September 7 cleanup audit | 403 backend passed, 1 failed, 4 skipped with live diagnostics disabled; separate 167 Search and 10 snapshot checks recorded | Partial semantic audit and historical tests, not current all-suite correctness |

Flutter analyze remains user-run. Existing Cupertino font warning was not a web-build failure. Documentation cleanup did not rerun application tests or deployment.

### Latest artwork/UI audit and user evidence

New screenshots show populated Home and recognizable track labels, supporting recovery of the original empty/Unknown report. Read-only AWS audit found seven rows, two ready successors, nine mix cards and complete track labels. Full artifact Recommended Artists contains 48 cards (paged serving may show ten), only four portraits. All 76 unique thumbnail URLs fetched HTTP 200, but inspected Last.fm PNGs are actual white-star placeholders. Last Played has artwork URLs for 11/13 tracks. Today's Pick ignores supplied art in web.

There are 79 stored raw albums, none qualified under current verified playback coverage, and no qualified album shelf. Radio supply has six cards and incomplete discovery/artwork contracts. Android's animated album hero is not ported. Detailed counts, code boundaries and comparison remain in KNOWN_ISSUES.

A fresh AWS `when september ends` took 54.13 s and ranked Amel Music before Green Day. A passing local Green Day fixture or repeated personalized query is not cold first-query acceptance.

### Accepted versus still pending

- Accepted/observed: Android related-song playlists, Search scrolling, New Releases baseline; browser playback on PC/iPhone in earlier sessions; ordinary signed-in login and now recognizable populated Home.
- Pending: full artwork/relevance and album/radio details; three promotions/ready-depth recovery; Search first-attempt/cold latency/private preference/isolation; full account/library flows; Android likes/settings scope races; playlist atomicity/two-device/offline rollout.
- Pending device evidence: cold/warm loading during lock/app/tab changes, Bluetooth/manual locked resume after pauses, 30–60+ minute sessions and real failure/retry. Desktop success is not iPhone acceptance.
- Deferred: remaining Charts/Releases/lyrics/recognition/assistant and advanced UI ports, cross-device handoff, PWA/offline, native/self-update gates and optional cleanup. All 66 task IDs remain in NEXT_STEPS.

## Historical milestones retained for context

- September 24 catalog/Home was deployed; guest Home was sampled populated with five rows / 108 items and two ready successors.
- September 26 live Search checks: Stairway to Heaven/Led Zeppelin, When Doves Cry/Prince, seak and desotry -> Seek And Destroy/Metallica. Prince album credits and 1,247-character biography were served; discovery A/B varied while repeated A remained stable.
- September 28 Oracle Home version 5 served six rows including 13 radio cards; radio reservoir 22 cards / six discovery seeds. Earned history admission, explicit-selection query context and artist-owned portrait reuse were deployed.
- September 29–30 Oracle served five Featured Albums with persisted detail depths 13/12/12/8/11. Portraits varied by artifact: an older active 1/48, prepared successors 10/48 and 7/48. These are historical, not current AWS readiness.
- September 25 Under Pressure logs were lost in September 26 container replacement. No later healthy transfer proves that failure's cause.
- September 20 old Home HTTP 200/preparing_initial with zero rows was not populated-feed success; the AWS restore and subsequent fixes supersede that checkpoint.

## Continuation

Read the full queue before selecting implementation scope. Current priorities are root artwork/artist/album supply fixes, Home presentation and freshness, Search/discovery acceptance, account/playlist ownership and device playback gates. Preserve AWS runway and restore-capable off-host backups. No new architecture or broad cleanup is required to begin focused work.
