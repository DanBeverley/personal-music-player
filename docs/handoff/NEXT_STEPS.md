# Next Steps

Reconciled November 5, 2026. **61 open IDs and 11 closed IDs, 72 tracked.** Open items include device validation, investigations and deferred proposals. [CURRENT_STATUS](CURRENT_STATUS.md) owns serving versions and test evidence. [KNOWN_ISSUES](KNOWN_ISSUES.md) owns investigations. [OPERATIONS](OPERATIONS.md) owns rollback.

**Serving:** `2026-11-06-artist-home` adds the artist overview repair and queues, returning Home shelf storage and large-artwork and Brotli changes. Chrome's playback card was restored on the inspected Android phone after enabling app-wide Chrome notifications. Separate physical lock-screen playback and resume checks remain open. CURRENT_STATUS owns the benchmark and verification.

**Priority:** P0 reliability; P1 release/safety; P2 maintenance/parity; P3 deferred. **State:** Fix = code needed; Validate = implemented, acceptance open; Recheck = investigate first; Decide = choice pending; Deferred = outside current scope; Done = closed engineering work.

**Storage completed — T07 closed:** backup capacity/reporting, safe retention and lossless storage conversion pass live checks. All 613 retained inventories and 45 database tables verify; physical database size fell 28.55 → 5.04 GB, with about 41.82 GB free. A normal 1.71 GB backup verifies, the actual retention service applies with no eligible backlog, and worker/timers are restored. Three verified daily copies and the pre-conversion recovery archive remain protected. Off-host/full restore and hosting migration stay deferred; sustained worker-memory and lane failures remain O02.

Historical release summaries have been removed from this queue; their detailed checks remain in CURRENT_STATUS. Read the entries below for current scope and completion checks.

## Finished engineering — removed from implementation work

- **Resources:** worker scheduling reads snapshot metadata without loading duplicate bodies; blocked radio retries back off. Restart preserves the real-use clock. Existing cache expiry/idle policy and 2.25/3 GiB API/worker limits remain. O02 retains sustained memory acceptance; no 4 GiB move is approved by measurements.
- **Lifecycle:** cache expiry, single/coalesced model-training ownership, optional-work idle mode, disabled legacy capture, guarded retention and first natural cleanup are deployed. Restore/downsizing remains O02–O05.
- **Search:** legacy replacement/inventory/paging and explicit-submit search remain. New report reopens only S01 exact artist Top/S05 artwork; queue stale-empty observation is D02. No architecture restart or restored filtering layer.
- **Playback/artwork:** indexed cached audio, pinned ranges/prefixes, position-preserving recovery, supplied portraits/cache publication and album/artist relationships are implemented. New staged-start parity and image-loader gaps remain P06/H01/S05; earlier implementation does not establish acceptance.
- **Discovery/UI:** pool/recycling balance, Home/vinyl redesign, integrated queue/search/lyrics and playlist controls are implemented. Charts/New Releases already have web shelves/detail navigation.
- **Acquisition/preparation:** owned artist IDs, persisted catalog reach, capacity-aware source verification, busy deferral, minimum-proof funding and proof-progress continuation are deployed. Preparation no longer waits for inventory projection; redundant radio exposure writes and repeated validation/history work are removed. Natural refill, diverse promotions and end-to-end timing remain D01/H04/H07/H08/G01.
- **Lock-screen artwork:** shared valid artwork selection, bounded fallback and current-track guards are deployed; compiled browser checks pass. Physical iPhone acceptance remains P01.
- **Playlist atomicity:** tested migration is installed on live Supabase by explicit approval: five functions, two revision triggers and RLS verified; all three playlists/30 tracks preserved. Anonymous reads return no rows and edits are rejected. Real two-device acceptance remains L01/L04/L05.
- **Storage/cleanup:** canonical pool storage, bounded shared profile ownership, lazy detail/Library rows and web-only native-image exclusion are deployed. Live cleanup/conversion/compaction and normal backup/retention checks passed; T07 is closed. Remaining native cleanup is T05/T06; off-host/full restore remains O04/O05.


**Now deployed:** H09 history counting, P04 silence incident reconstruction, H07 radio scheduling/count diagnostics, provider-block reason preservation and duplicate waiting supply-job prevention. Post-deployment checks are distinct from physical acceptance. Android account switching needs confirmation; backend CI is unchanged. No main merge is authorized.

## Current order

1. **P0 — P02/P03/P06, validate deployed handoff repair:** user tests initial cached selections, natural locked queue changes on Android Chrome/iPhone, pause/resume and rapid selection/Pause. Automated/live evidence is positive; do not treat desktop gating as physical Android acceptance. The system card and fully buffered native pauses remain OS-level rechecks if reproduced. Worker memory/lane failures stay deferred under O02; other feature work waits for results.
2. **P1 fixes — H07/H08/A05/A06/T04:** remaining recommendation coverage and preparation delays; confirm Android account-switch problems; add backend CI. H09/P04 code is deployed and awaits usage checks below.
3. **P1 device and Home investigations — P01/P02/H10/D01/H04/H08/H15/H17.** Check lock-screen controls and resume, longer phone sessions, usable new-artist albums, shelf depth and time to usable Home. H16 records the requested queue behavior for the four track rows. W05 and W09–W11 capture the later artist, player, assistant and sidebar proposals.
4. **P1 safety — A01–A06/L01/L02/L04/L05:** account/session isolation, Android ownership fixes, concurrent playlist edits and sync/retry acceptance. Detailed scope is below.
5. **Hosting O03 is deferred:** the user will return to Lightsail later. Retain 8 GiB and the existing cost evidence. Android automatic updates (G05), test cleanup and README/release review remain P2/P3. Off-host backup/full restore (O04/O05), optional ports, PWA/offline and device handoff remain deferred.

## Size and resource work — completed scope and follow-ups, November 4

These five tasks expand T05/T06/T07 without adding IDs. The resource code and live storage repair are complete. CURRENT_STATUS owns measurements; remaining cleanup and device acceptance stay separate.

1. **Done / T07 — Backup and storage repair.** Checkpointed streaming recovery avoided impossible full staging. Capacity reserves, owner-correct SQLite sidecars and nonzero failure reporting pass. Reference-aware cleanup, 45-table/value/schema comparison, physical compaction and actual scheduled online backup/retention all passed. Three verified daily copies and the pinned pre-conversion archive remain.

2. **Done / T07 — Oversized writes repaired.** V3 bounds canonical bytes at 64 MiB and logical recovery at 128 MiB; over-limit enabled writes fail explicitly. Generated 69.2 MB logical data stores as 202 KB with exact recovery. Live conversion passed; none of the 335 retained candidate snapshots exceeds 64 MiB. Older pre-v3 image rollback requires expanded recovery.

3. **Done / T07, T06 — Pool-body duplication repaired.** Exact item bodies share storage; scores, ordering, editions, playback proofs and historical hashes survive. All 613 retained records verify. Candidate snapshot storage fell 5.03 → 1.29 GB; working candidates fell 225.45 → 119.26 MB. Application reads keep independent mutable occurrences.

4. **P2 / T06 — Profile duplication repaired.** The second expanded cache is removed. Existing bounded SessionStore owns expiry; compact facts restore aliases and borrow the matching global model. Generated20-profile expired retention5.55MB→zero; serialized bytes9.00MB→2.99KB. Model-change/invalidation and existing recommendation tests pass. Sustained process memory remains O02 acceptance.

5. **P2 / T06, T05 — Web rendering repaired; further cleanup remains.** Details project once per accepted response. Lazy album/artist/Library rows retain original selection/filter/account guards;500 songs initially build3 phone/5 desktop rows. Unread HomeTrackStrip callbacks are removed. Native branding is excluded from web while retaining Android assets. Native progress subscription narrowing, other confirmed unused inputs and duration-format consolidation remain. Physical phone smoothness still needs validation; no additional large module is proven disposable.

Further measured follow-up under T06: reuse warm model signatures and operation-local Home facts; reduce empty worker polling without delaying accepted work. Smaller-host suitability needs sustained loaded measurements. Source cleanup and idle CPU reductions alone do not lower the current fixed EC2 hourly charge; hosting changes remain O03.

## P1 fixes — scope and completion checks

| ID | Fix scope | Completion check |
| --- | --- | --- |
| **H09** | Deployed: count actual plays independently of later skips; keep exact recording/source identity through metadata updates. Rebuild computed counts without deleting events. | Check real count changes and descending order after repeats/reload/another device; equal names alone cannot merge recordings. |
| **P04** | Deployed: silence bookmarks survive full archive reconstruction once; later unavailable timing cannot erase earlier transfer evidence. | Mark silence, export/reload/share/copy and verify current-session evidence. Investigate separate archive-write failures only if reproduced. |
| **H07** | Deployed: album shortages request related artists, relationship evidence survives missing tracks, and cooling artists do not consume the eligible limit. Proof work now balances familiar/new artists. | Check natural new-artist qualification and real feed open/play. Follow-up still finds eight source-safety rejections; locate their row provenance without weakening safety. |
| **A05** | Possible Android like-save race: account identity is read again after local I/O. Confirm before implementing ownership guards. | Delay saving, switch A→B and check which account receives the action; also test sign-out and A→B→A. |
| **A06** | Possible Android settings race: late loads/saves can survive an account switch. Confirm before implementing reset/response guards. | Switch accounts with a delayed settings response; A's result must not replace or save B's settings. |
| **T04** | Backend CI means reproducible automatic tests on PRs/changes. Local release checks exist; checked-in workflows lack backend pytest and the manual legacy deploy targets OCI. Retain the needed test fixtures in a fresh checkout, add isolated deterministic CI and reconcile deployment separately. | Clean checkout passes without production secrets/state/provider calls; failing tests fail CI. Keep opt-in live/device gates and disabled legacy deployment separate; adding CI does not authorize deployment. |

## Accumulated target testing checklist

Passed automated/server checks do not need manual repetition. Physical checks below remain open after deployment. Suites overlap, so their totals must not be added together.

| Target | Status / what establishes acceptance |
| --- | --- |
| Web account isolation and playlist failure handling (A02/A03/L01/L02/L05) | **Automated passed:** 117-check safety run; late responses, queued writes, conflict/retry, lost acknowledgement, reload and scoped history/queue contracts. |
| Database transactions and access restrictions (A02/L01) | **Passed:** 11 isolated PostgreSQL checks; live approved migration, five functions/two triggers/RLS, preserved rows and anonymous access checks. Actual two-device editing remains open. |
| Playlist interface (A01/A04/L04) | **Compiled passed:** synthetic sign-in and create/rename/delete/add/remove/reorder at 1440/390/320 px, current audio retained. Actual account/device behavior remains open. |
| Playback/navigation/reload (A04/P01/P02/D02) | **Compiled passed:** one generated-audio element, Home/Library/Search navigation, queue edits, seek/pause/resume, automatic transitions and saved-position restoration. |
| H09/P04/H07/P06 regression contracts | **Passed and deployed:** latest selected exact-release gate941 backend/257 frontend checks plus12 compiled stages; optional/excluded cases remain explicit in CURRENT_STATUS. Provider-block control retains one attempt/correct message. |
| Long physical playback (P03) | **Reopened:** hnkiend43k passed, but latest hnlmcs9sdc records Chippin' In twice/YAD once failing mid-track. Verify original and indexed media through slow/interrupted public delivery and hidden recovery; no new long-playback acceptance yet. |
| Locked resume/Bluetooth (P01/P02) | **Device pending:** Play after 0/5/30/120-second pauses, with/without Bluetooth; retain position and explicit Pause, avoid repeated reloads. |
| Cold/warm delivery (P06/P05) | **Device/server pending:** identify cold versus cached starts and compare the exact source/version when comparing devices or a retained 505; no universal sub-three-second claim. |
| Artwork (P01/H01/H03) | **Device pending:** Home/Search/details and native lock-screen covers, missing/placeholder fallback, delayed enrichment and track switches; no previous-track cover or album-art substitute for artist portraits. |
| Recommendation quality (D01/H02/H04/H06–H08) | **Still open:** three promotions occurred, but first three feeds have no unheard album artists and heavy shared shelf artists; latest improves. Validate useful similar/new artists, varied artist works, distinct album shelves and fresh usable radio. |
| Feed promotion (G01) / preparation (H08) | **Promotion passed:** 97→98→99→100 once each, ready reserve remains one; fresh server check has no duplicate live jobs/generations/running groups/expired leases. **Speed remains H08:** candidate runs reach130 s and composition40 s; version changes alone do not accept quality. |
| Frequently Listened (H09) | **Device pending on deployed build:** compare count changes and descending order after repeats, reload and another device; seeks/retries/saves must not inflate plays, later skips must not erase them. |
| Diagnostics (P04) | **Device pending on deployed build:** mark silence; full current-session export retains incident, earlier delivery and >262-KiB evidence after reload; Share/copy/download work and old runs stay opt-in. |
| iPhone rendering and navigation (H10–H14/D02) | **Partly passed:** vinyl/cover queue feels good to the user. General browsing still lags and needs frame/image measurements; keep lyrics/search/edit/Discover more/dedicated pages/large text/reduced motion checks where not reported. |
| Real authentication (A01) | **Device pending:** email/Google signup/login, recovery/expired link, session reload and sign-out on PC/iPhone. Synthetic sign-in is insufficient. |
| Real account/guest switching (A02/A03) | **Device pending:** switch during loads and reload; likes/playlists/feed/queue/settings belong to the current account only. Confirm the Android risks under A05/A06 and fix if reproduced. |
| Real concurrent editing/sync (L01–L05) | **Two-device pending:** concurrent rename/add/remove/reorder preserves both edits or reports a conflict; check duplicates, offline/partial failure/retry and remote-only playlists, including Android. |
| Android automatic updates (G05) | **Pending:** the app notices a newer version, downloads the correct APK, checks the file and opens Android's update installer; verify success, failure messages and retry. Ordinary APK installation is already accepted. |
| Server acceptance (O01/O02/G01/H07/H08) | **Checked:** live playlist schema, aggregate job ownership, new persisted supply and work timings. Before rollout API used 0.61/2.25 GiB, worker 2.29/3 GiB, with no OOM/restarts. **Still open:** retained allocations under sustained workload, full user-request preparation time and genuine diverse promotions. Keep 8 GiB. |
| Infrastructure (O03–O05) | Lightsail O03 and off-host backup/full restore O04/O05 are user-deferred. Retain pricing evidence; any later migration still needs restore/load validation. |

## Playback and diagnostics

| ID | Priority | State | Remaining action |
| --- | --- | --- | --- |
| P01 | P1 | Validate | Test iOS cold/warm playback through lock/tab/app changes; accept deployed Media Session artwork/fallback and correct covers across track switches. Audible track and state must agree. |
| P02 | P0 | Validate / Recheck | Local candidate restores current metadata on confirmed native resume and captures media state before delivered commands. Chromium controls pass. Compare Chrome and Brave on the same Android device. On iPhone Brave, test Play from the card, headset and app after short and longer pauses, including the reported roughly 15-second interval. Correlate callback, native Play and advancing audio. Locked queue advance stays under P03. |
| P03 | P0 | Validate / Recheck | User confirms locked queue advance on Android Chrome. Keep iPhone natural changes/rapid selections pending. Actual next-one buffering and bounded native recovery remain deployed. Animal's fully buffered native Pause remains a separate OS-level recheck; no blind auto-resume loop. |
| P04 | P1 | Validate | Local archive export adds bounded failure category/writesRecovered; failed batches were already retained/retried. One historical failure/complete=false alone cannot prove missing rows. Candidate timing/Search-image identity retained without high-rate tracing; device export responsiveness remains. |
| P05 | P2 | Validate | Compare retained Android/web 505 failure using identical source, endpoint and cache version; matching title is insufficient. |
| P06 | P0 | Validate | Initial cached direct handoff and hidden full-cache adoption deployed. Live16/16 initial start/seek/resume trials: Chromium1.983→1.758s, desktop WebKit2.333→1.866s. No mandatory metadata round trip, legacy/version guards retained. Validate physical initial/next/previous and cold tracks; cold misses still use existing acquisition and first-format preparation. |

## Memory, hosting and recovery

| ID | Priority | State | Remaining action |
| --- | --- | --- | --- |
| O01 | P2 | Validate | Verify stable public IP and origin/auth callbacks before any EC2 stop/start or resize. |
| O02 | P1 | Fix / Validate — later | Kernel confirms recommendation-worker memory-limit kill at08:33:57UTC November2 (anonymous RSS~2.99GiB). At08:49 a database-lock exception while writing status escapes and terminates a Home lane. Bound retained/expanded inventory and model memory; make status reporting failure-safe so a temporary lock cannot kill a lane; test sustained workload/recovery. User defers this repair until after playback/performance. Earlier duplicate-read improvement remains valid; keep8GiB and existing caps. |
| O03 | P3 | Deferred | User postponed Lightsail October 27. Retain cheaper 8 GiB hosting/cost evidence; any later move needs account/plan approval and isolated restore/load/playback checks. No resize/migration is performed. |
| O04 | P3 | Deferred | Off-host runtime/configuration backup remains user-deferred. Local backup capacity, truthful failure reporting, retention and compaction passed under closed T07; both on-host timers are enabled. |
| O05 | P3 | Deferred | Temporarily postponed by user October 26. Full isolated service/config/cookie/auth/Caddy/Supabase/R2 restore remains unverified; partial reader/image recovery already passes. |
| O06 | P3 | Decide | Decide whether post-September-20 Oracle data needs recovery; preserve its stopped disk until decided. |

## Account and playlists

Web playlist editing is explicitly user-authorized and enabled. Preserve it; concurrency/two-device acceptance remains open.

**P1 safety scope:**

- **Account isolation (A02/A03/A05/A06):** guest and each signed-in account own separate library/feed/queue/settings state. If account A starts a request and the user switches to B, A's late response must not appear in B's UI or write into B's storage. Check ownership again after asynchronous work; clear/reload scoped state on auth change. Android like writes and recommendation settings have retained defects; broader web/account acceptance remains validation.
- **Playlist concurrency (L01/L04/L05):** installed live transaction/revision schema is verified. Two devices must preserve edits or report a conflict; check real-device retry, duplicate tracks and ordering.
- **Sync and session behavior (A01/A04/L02):** verify login/recovery/sign-out, reload persistence, offline/partial-failure retries and remote-only playlist preservation. Navigation must keep the selected playback order/index and one audio source.

**October 26 acceptance:** prior 117 Flutter/11 isolated PostgreSQL checks and compiled playlist flows pass. Live migration is now applied and verified, preserving all rows. Actual auth, Android sync and physical two-device editing remain open. CURRENT_STATUS owns receipts and limits.

| ID | Priority | State | Remaining action |
| --- | --- | --- | --- |
| A01 | P1 | Validate | Local auth validation and compiled synthetic email sign-in pass. Actual PC/iPhone email/Google signup, recovery/expired-link, reload and sign-out remain; normal login is user-observed. |
| A02 | P1 | Validate | Isolated SQL account-owned likes/search reads and writes, web library mapping and scoped reload contracts pass. Live cloud reload/order and Android refresh remain. |
| A03 | P1 | Validate | Web late reads/acks, queued mutations, account-scoped queue/checkpoints and guest history tests pass. Integrated real account/guest switching and settings acceptance remain; Android defects stay A05/A06. |
| A04 | P1 | Validate | Compiled Home/Library/Search navigation and playlist edits preserve one audio source; queue edits/reload preserve order/index/position. Physical signed-in likes/album navigation acceptance remains. |
| A05 | P1 | Recheck / Fix | Confirm the Android like-save race: start under A, delay saving, switch to B and check where the action ends up. The code permits this; physical reproduction is pending. Fix ownership if confirmed. |
| A06 | P1 | Recheck / Fix | Confirm whether A's late recommendation-settings response replaces B's settings after switching accounts. Code has no switch guard; physical reproduction is pending. Reset/reload and reject old responses if confirmed. |
| L01 | P1 | Validate | Isolated concurrency/revision/rollback/order checks pass. Live migration/functions/triggers/RLS and row preservation verified; physical concurrent-client acceptance remains. |
| L02 | P1 | Validate | Local room/queue library-revision subscription repaints optimistic likes and rollback before delayed acknowledgement; tests pass. Repository ownership/reconciliation retained. Physical responsiveness and Android sync/offline/remote-only acceptance remain; not yet deployed. |
| L03 | P2 | Fix / Validate | Build/install main-tree Android delta sync; test two-device order/ownership. Do not assume worktree parity. |
| L04 | P1 | Validate | November2 deployed picker adds named creation/date display, save from song/player surfaces and atomic create-with-first-song.139 client/12 embedded SQL checks and compiled three-width creation/addition/audio pass; previous rename/delete/reorder coverage remains. Matching functions are installed with3 playlists/30 tracks unchanged; real two-device acceptance remains. |
| L05 | P1 | Validate | Enabled editing preserved by approval; disabled-gate/missing-RPC rejection and isolated conflicts pass. Physical concurrent/two-device acceptance remains; retain configurable rollback. |

## Search and queue discovery

| ID | Priority | State | Remaining action |
| --- | --- | --- | --- |
| S01 | P1 | Validate | October31 provider-identity/Top corrections are deployed; all provider songs remain available. November2 local suggestion merge preserves saved playable cards. Broader real-query/device acceptance remains; no Search architecture replacement. |
| S05 | P1 | Validate / Recheck | Typed profile/portrait acquisition and autonomous bounded metadata work are deployed, preserving all tracks and separate contributor credits. Public refreshed snapshots/decoded portraits improve; old snapshots/provider failures may still delay replacement. Verify physical missing track/artist artwork; historical specific failure remains untraced. |
| D01 | P1 | Validate | November1 identity-based coverage demand and additive bounded acquisition are deployed. Confirm newly listened/related artists produce qualified songs/albums and usable paged shelves without discarding familiar inventory. Held for the user's later session report; planner output is not completed acquisition. |
| D02 | P1 | Validate | Local empty snapshot freshness5s and bounded picker revision observation/truthful failure/pagination replace stale-empty single-read/six-row cap. Focused tests pass; physical Search/play/add/append-position acceptance remains. |

## Home — deployed code, remaining acceptance

| ID | Priority | State | Remaining action |
| --- | --- | --- | --- |
| H01 | P1 | Validate | Deployed bounded image I/O/CPU separation, shared variants, prepared sizes and truthful source/storage outcomes. Live8-image batches~883→187ms, all valid; invalid HTTP responses close and good-source storage faults do not incur6h bans. Verify phone artwork arrival/consistency. |
| H02 | P1 | Validate | Inspect fresh artist relevance using identity/relationship/profile evidence; popularity alone cannot admit results. |
| H03 | P1 | Validate / Recheck | Existing Home repair now retains identity/source evidence, specific failure stages and true next-eligible retry time; cooldown-only claims do not spend attempts. Validate naturally repaired Home portraits. Existing cooldowns are not globally erased; no borrowed album art or hidden cards. |
| H04 | P1 | Validate | Open/play persisted albums with distinct matched sources: all tracks below six, otherwise at least six and 80% coverage. |
| H05 | P2 | Validate | Observe seed/frontier progress, exhausted-source exclusion and cursor wrap; recommendations must not count as listening. |
| H06 | P1 | Validate | Verify fresh radio title/artist/source/index and physical deduplication; preserve old immutable artifacts. |
| H07 | P1 | Validate | November1 recent/uncovered-artist demand and interleaved familiar/recent seeds are deployed through the existing worker. Await actual qualified supply and diverse artist works/album rows in the user's later session; keep bounded ownership and additive inventory. |
| H08 | P1 | Validate / Recheck | Home readiness/deadline and atomic heartbeat/bounded SQLITE_BUSY containment are deployed; old-clock and real WAL lock/recovery controls pass. Prior source-safety rejections/state conflicts/full preparation timings and natural workload acceptance remain; preserve proof requirements. |
| H09 | P1 | Validate | Backend/browser counting fixes deployed and regression checks pass. Accept actual count changes/order across reload/devices; same titles alone do not prove identical recordings. |
| H10 | P1 | Recheck / Validate | Baseline frame stalls, decoded artwork and retained memory on the same phone during a repeatable Home scroll and 30-minute listening session. Prioritize lazy shelf construction, artwork sized to the card with limited ahead-of-scroll warming, and suspension of offscreen motion and unnecessary refresh work. Preserve the current feed and scroll position. Compare frame and image timings, memory and battery with matched brightness, playback and browsing steps. |
| H11 | P2 | Validate | Accept Today's Pick cover/readability and selected/audio agreement at phone/desktop widths and large text. |
| H12 | P2 | Validate | Accept reflected hero/dots/open/play, seven-second wrap and reduced-motion/inactive pauses; check distinct featured/recommended albums. |
| H13 | P2 | Validate | Check responsive circular artists and distinct radio/mix/collage artwork; no invented counts. |
| H14 | P1 | Validate | Accept recent/frequent lists, menus/likes/paging and dedicated row pages; preserve order and scoped state. |
| H15 | P1 | Validate / Recheck | Local candidate has eight-track recommendation previews, lazy expanded shelves with automatic first expansion paging, and an admitted artist reserve for Because You Played. Validate qualified supply, saved shelf size and rendered unique items on a fresh real feed. Deeper themed row generation and long-session resource measurement remain open. |
| H16 | P1 | Validate | Local candidate starts the selected song and fetches its own recommendation queue from Frequently Listened, Last Played, Quiet Picks and Because You Played on Home and expanded pages. Keep explicit collection sequences. Fixture passes paging and selection beyond the initial list. Verify first-play latency and queue continuity on device. |
| H17 | P1 | Validate / Recheck | The deployed Home fix saves complete shelf pages within the existing per-account limit and shows the Home shell at startup. Verify returning Home on phones after one successful load, then measure first visits separately. First visits still wait for the server. |

## Product parity and retained Android gates

| ID | Priority | State | Remaining action |
| --- | --- | --- | --- |
| W03 | P2 | Validate | October31 stationary vinyl, right-side chat and account/song-local replies/drafts are deployed. Compiled retention/geometry/audio pass; broader signed-in iPhone keyboard/return-to-song acceptance remains. November2 playlist UI is deployed; G03 owns new reply-tone refinements. |
| W04 | P3 | Deferred | General recognition/assistant port stays deferred; shared assistant/tool audit for song conversation is completed under W03/G03. Do not duplicate that flow or treat conversation memory as external evidence. |
| W05 | P2 | Validate | Deployed artist profile crash fix, optional section isolation, deferred persistence, release metadata and artist queues. Album-supplied counts work, while YouTube challenges the player count fallback from AWS. Verify marzuz and Metallica on devices, including cold/warm open, selecting song five and exact artist Search playback. Paul Owen Music hero and James Hetfield exact-credit acceptance also remain open. |
| W06 | P3 | Deferred | Design explicit device playback handoff and conflict/ownership rules; local persistence is not handoff. |
| W07 | P3 | Deferred | Implement installable PWA/update lifecycle; verify iOS behavior. |
| W08 | P3 | Deferred | Implement scoped browser offline downloads/eviction/playback, separate from shell caching. |
| W09 | P2 | Validate | Local candidate uses an inline desktop volume slider. Check pointer and keyboard input at compact desktop widths. |
| W10 | P2 | Validate | Local Library chat offers playable suggestions and playlist drafts/additions for review. Explicit rename, remove and move commands use existing cloud writes. Validate live model cards, edits and conflict handling with two devices. |
| W11 | P2 | Validate | Local desktop sidebar opens saved playlists and followed artists. Artist follows persist per account in the browser. Validate phone exclusion and account-switch isolation. |
| G03 | P2 | Validate | Local song chat preserves retrieved evidence through lookup failures and labels uncited articles as related reading. Validate live factual and interpretive questions against their linked sources. |
| G05 | P2 | Validate | Android automatic updates: verify the app finds a newer release, downloads the right APK, checks it and opens Android's update installer. Check successful update, useful error messages and retry. Keep this one update task; ordinary APK installation is accepted. |

## Tests, cleanup and documentation

| ID | Priority | State | Remaining action |
| --- | --- | --- | --- |
| T02 | P2 | Fix / Validate | Three unchanged Android failures reproduced: personal mix detail, recommendation feed models and track menu. Reconcile tests/product contracts without weakening behavior coverage. |
| T03 | P2 | Recheck | Narrow mocked composition-budget claims to publication overhead; retain separate real-composition timing. Label mocked New Releases detail checks as structural, not source-playability acceptance. |
| T04 | P1 | Fix / Recheck | Local deterministic release gates exist; add applicable backend CI and reconcile obsolete deploy host/overlays. Provider probes remain opt-in; device acceptance stays separate. |
| T05 | P3 | Recheck | Previous confirmed removals pass. New audit identifies unread client inputs and obsolete batch worker wrapper/helper; migrate active scheduler assertions before retirement. No further large client module is proven dead; preserve distinct Android/web ownership and rollback. |
| T06 | P2 | Fix / Recheck | Deployed: exact pool-body deduplication, one expiring profile owner with shared model references, one accepted detail projection and lazy detail/Library rows. Remaining: warm model signature scans, native progress subscriptions, other confirmed unused inputs and shared duration formatting. Check sustained memory under O02; do not split files blindly or merge native/web playlist stores. |
| T08 | P2 | Fix / Validate | README overview/demo/setup exists; refresh stale live-search/playlist/report claims, obtain user review and reconcile release workflow before main merge. |

## Closed IDs — excluded from the active queue

| ID | State | Closure |
| --- | --- | --- |
| G01 | Done | Three real exactly-once promotions 97→100, reserve remains one and fresh server audit shows no duplicate live jobs/generations/running groups/expired leases. Discovery quality and preparation speed remain D01/H07/H08. |
| S02 | Done | User reports no Search usage defects and removes remaining Search acceptance checks, October 26. |
| S03 | Done | User accepts current Search usage, including recent-search behavior, October 26. |
| S04 | Done | Remaining Search checks removed by user acceptance, October 26; unreleased experiments are not included in this acceptance. |
| S06 | Done | User accepts Search latency, October 23. |
| W01 | Done | Charts accepted by user with no usage defects, October 26. |
| W02 | Done | New Releases accepted by user with no usage defects, October 26. |
| G02 | Done | Duplicate Charts acceptance check closed with W01, October 26. |
| G04 | Done | Android APK installation works according to user, October 26. Only automatic update remains under G05. |
| T01 | Done | Incorrect readiness assertion corrected in both trees; distinct persisted-source coverage retained. CI remains T04. |
| T07 | Done | Local backup and failure reporting repaired; reference-aware cleanup and lossless conversion passed. Verified compaction reduced 28.55 → 5.04 GB; actual normal backup/retention checks passed and timers/worker are restored. Recovery archive preserved. Off-host/full restore remains O04/O05; worker memory remains O02. |

## Verification boundaries

- User runs `flutter analyze`. Fixtures/compiled checks do not establish physical-device acceptance.
- Device failures need build/UTC/actions and current-session report; compare exact source/version/cache state and actual audio.
- Preserve active/ready/job references, audio/private state and rollback. Aggregate audits are authorized; sensitive account/heap access needs permission.
- Task listings alone do not authorize deployment, account mutation, host resize or main merge. Previously approved releases are recorded in CURRENT_STATUS/OPERATIONS; main still requires the user's greenlight.
