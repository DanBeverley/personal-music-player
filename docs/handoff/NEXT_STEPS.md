# Next Steps

Consolidated October 3, updated October 5, 2026. This is the complete retained queue: **66 tracked entries across ten areas**. Read CURRENT_STATUS for shipped work, KNOWN_ISSUES for diagnostic evidence, ARCHITECTURE for contracts and OPERATIONS for hosting/backup details.

Current AWS build is `2026-10-05-refined` at https://neatie.duckdns.org/, with H01–H14, album transport and the quiet minimal desktop-first frontend plus motion/controls/browser-branding refinement. Application source is committed through 7553f5b; tests/runtime/private files remain excluded and nothing was pushed. The October 4 transport correction passed 95 focused checks and direct deployed-worker media verification with master cookies unchanged. That day's normal worker audit found 66 fresh source proofs and three of 110 albums passing coverage, but zero persisted qualified inventory; radio had nine reserve/eight visible cards failing depth/discovery. These are dated observations, not an October 5 supply audit. Fresh publication/open/play, real-account and iPhone acceptance remain open.

## How to use this queue

- **Fix:** implementation remains needed or a confirmed code defect remains.
- **Validate:** implemented/deployed behavior still needs live, account or device acceptance; do not implement it again merely because its checkbox is open.
- **Recheck:** an older audit/test finding has no documented closure; revalidate current code before changing it.
- **Decide:** a product/hosting choice remains; listing it does not authorize external actions.
- **Deferred:** retained roadmap/cleanup, not immediate release scope.
- Combined labels mean both engineering and acceptance remain. IDs identify entries, not estimates of effort; an entry may contain several related checks.

Immediate focus is unresolved H01–H08 supply and H01–H14 device acceptance. A05/A06 are retained Android account-ownership defects and must be handled before expanding account mutation rollout. L01–L05 gate playlist writes. Device tests remain pending even when source fixes passed automated tests. Hosting runway O01–O05 remains operationally important.

October 4 continuation: H01–H14 are deployed with rollback and public build/hash/health verification. H05–H08 fixes cover album frontier/cursor persistence, semantic radio source joins, admitted discovery seeds and identity-owned artwork/actual-artist novelty. The eight supply entries remain open until source recovery, published album/radio open/play and three live rotations pass. Fresh inventories confirm seed progress, but all 98 albums fail verified source coverage; radio depth/discovery also fails. Worker backoff remains intact. See CURRENT_STATUS for exact scope, 288 backend/81 Flutter checks and public browser navigation. H09–H14 device/cloud acceptance is pending; fixtures do not establish it.

## Accumulated H01–H08 acceptance checklist

Test on desktop and Brave iPhone using AWS build **2026-10-05-refined** at https://neatie.duckdns.org/. Confirm the desktop footer or Diagnostics build after reloading. Use both your signed-in account and a separate guest session. Capture build, UTC time, screenshot, action sequence and copied Diagnostics for failures; do not include credentials. Existing immutable feeds can retain older artwork/metadata until the backend promotes a genuinely newer feed. The new browser origin requires fresh sign-in; old-origin guest history/queue remain on the old address.

| ID | Device actions and expected result | Server acceptance still needed |
| --- | --- | --- |
| H01 | Inspect track/mix artwork in Home, Search and details, then reload and revisit. No white-star provider placeholders; valid covers persist and alternate artwork is attempted when an image fails. Today's Pick's visible cover also has H11 checks below. | Inspect newly built artifact metadata and stored repairs; an HTTP-200 image alone is insufficient. |
| H02 | Review Recommended Artists against actual listening/likes. Open unfamiliar recommendations and report obviously unrelated examples. Account and guest feeds must stay separate. | Confirm admitted identity/relationship/profile evidence; novelty or popularity alone cannot admit an artist. |
| H03 | Check genuine artist portraits across Home and artist detail; repeat after reload and a new feed. Missing portraits must remain honest; no song/album cover presented as an artist photo. | Verify exact-identity portrait repairs, including track-derived credits, and retain unresolved provider misses. |
| H04 | Open every published album, check cover/title/artist and usable track list, play individual tracks and Play album from the selected index. Record any source failure with Diagnostics. | Verify bounded transient-source retries and persisted detail: all tracks when fewer than six, otherwise at least six and 80% coverage. Zero qualified albums remains a supply failure, not permission to weaken publication. |
| H05 | Revisit albums after real listening/likes and newly prepared feeds; distinguish unchanged saved feeds from newly acquired supply. Browsing recommendations must not increase listening counts. | Verify supported album seed rotation, exhausted-provider exclusion, cursor wrap-to-zero and persisted progress. Device screenshots cannot prove these worker contracts. |
| H06 | Open several radios, inspect every track label and play different selected tracks. Selected title/artist, queue index and audible source must agree; no Unknown labels or duplicate physical songs under conflicting metadata. | Recheck canonical/source/version matching and physical-source deduplication in fresh radio inventory/artifacts. |
| H07 | Count Popular Radio cards and usable tracks, then open/play artist, discovery and mix radios. Other Home rows must still work while radio supply is incomplete. | Require 10–15 cards, at least eight playable tracks per card, at least three non-mix discovery cards and two verified mixes. October 4 later audit: eight visible cards; depth/discovery failed. |
| H08 | Record radio artists/order/artwork over **three genuinely distinct promotions**, waiting for ready successors before each Refresh. New artists should appear first; portraits belong to their artists and mixes use distinct genuine artwork. | Compare actual artist identities: at least three changed artists per rotation. Changed mix IDs or repeated button presses are insufficient. |

When Refresh shows its ready dot, tap Refresh once and record the before/after feed diagnostics. The separate ready-message control was removed. Do not spam Retry to bypass worker backoff. Album/radio absence is a recorded failure or blocked acceptance case, never an automatic pass. October 4's later audit still had zero persisted qualified albums and eight visible radios; a fresh audit must establish whether supply has improved. Non-sensitive database audits are authorized; sensitive access needs permission.

## H09–H14 deployed implementation and device acceptance

AWS build **2026-10-05-refined** retains H10–H14 and refines their motion/presentation; H09 adds deterministic verification of existing history/count behavior. The first redesign passed 68 focused checks; this refinement passed 31 focused Flutter checks and synthetic 1440px/390px/320px rendering/actions. Run the following device checks on this build; retain the H01–H08 supply gates. The album hero remains unavailable until qualified album supply exists:

| IDs | Device acceptance |
| --- | --- |
| H09 | In guest and account separately, play through actual progression, repeat the same song, reload, seek, pause/resume and Retry Home. A real repeat adds one play; seeking, restored position, browsing and Retry add none. Switch guest/account and accounts A/B, including during requests; no history/count leakage. Verify cloud merge/reload as well as local counters. |
| H10 | Scroll Home, visit Search/detail and return; also switch browser tabs or background/foreground the phone. Existing feed, scroll and browsing stay stable; a bounded queue-status check exposes ready successors without automatic promotion. Rapid returns must not multiply requests. Only explicit Refresh accepts a new populated feed. |
| H11 | Today's Pick displays its exact song cover or an honest missing-image state, readable song/artist text and a working play control at desktop/phone widths and larger text. Selected track and audio agree. |
| H12 | Qualified Featured Albums advance after seven seconds at rest, wrap forward from last to first and keep looping. Indicators and open/play must match the visible album before and after wraps. Manual swipes/indicators work; autoplay pauses while Home/browser is inactive or reduced motion is enabled. Without embedded preview tracks, Play opens usable album detail for a second explicit gesture. Empty qualified supply remains hidden. |
| H13 | Artists are circular with readable names; mix/radio cards are distinct; collages use distinct supplied artwork. Artist radios must not borrow unrelated track covers. No invented zero-song counts. Check portrait/card layouts with larger text. |
| H14 | Last played/frequent strips retain order and selected queue index. Likes/unlikes update Home and details across reload; track Options can play/discover, like and open supplied artist/album IDs. Verify mix/radio covers, metadata, Play all, ordered track taps and More paging. Only backend-supported Home lanes appear, with backend tracks/albums/artists. Playlist mutations remain disabled; assistant is W04. |

Refinement acceptance: inspect the favicon/Apple icon and startup equalizer, Home row-header arrows and Search's labelled paging buttons. Search input/filter/Top result must be spaced correctly. Test entry/artwork/control transitions with normal and reduced motion, then Home -> Search -> Home during playback: scroll, query, queue and audio must remain intact. Loading feedback must stay inside its relevant control/surface; Refresh's ready dot replaces the removed message. These checks extend existing H10–H14; all 66 queue IDs/statuses remain retained. Manifest branding does not close W07's PWA update/cache/device gates.

## Completed work and historical claims

- October 5 supplemental acceptance stays within the existing Home/playback/account entries: review desktop spacing/typography, sidebar navigation, daily pick, album indicators and artist/mix/radio/track presentation. Verify the dock's play/pause/seek, next/previous, likes, repeat and queue. On phone, expand/collapse the dock, repeat and open Queue, including long titles and larger text. Check Search results, Library and account flows retain their behavior after the restyle. The visual pass does not close H01–H14 or P01–P06 device/data gates.

- AWS x86 deployment and restoration of the September 20 runtime are complete, including SQLite integrity, HTTPS, public build/hash, desktop real playback/seek/reload and API/worker restart checks. Oracle is stopped; A1 is an optional alternative, not the current migration blocker.
- The October 1 Search/radio/history/startup batch and October 3 history metadata, initial Retry, mix-reservoir, metadata admission/bundle and Search timeout fixes are deployed. Do not queue a duplicate deployment or replacement Search/Home architecture.
- The latest screenshots support populated Home with recognizable track labels. Artwork, relevance, optional album/radio shelves and device parity are not thereby accepted.
- Everlong timeout preservation passed public Chromium; actual iPhone enrichment/tab-return acceptance remains S05.
- Ordinary user login and playback are observed. The full email/Google/recovery/isolation/library matrix remains A01–A04.
- Android related-song playlists, Search scrolling and New Releases baselines were accepted. Keep those baselines; investigate a fresh regression rather than redoing accepted implementation.
- September 29 five-album and 13-radio results belong to older Oracle runtime checkpoints. Current AWS album/radio supply does not inherit those results.
- The September 20 archive was used for the AWS restore. The newer downloaded AWS archive is hash/SQLite verified, not full-restore tested, and predates the latest history/Search fixes.
- The September 7 cleanup audit is partial and historical. Its proposed deletions, test rewrites and findings have no blanket completion claim. A05/A06 still match inspected account-scope code; other cleanup entries require current dependency/test checks.

Evidence: [current status](CURRENT_STATUS.md), [known issues and dated diagnostics](KNOWN_ISSUES.md), [architecture/contracts](ARCHITECTURE.md), [operations/recovery](OPERATIONS.md).

## Home data and interface

Evidence: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| H01 | Validate | Accept deployed placeholder rejection and persisted exact-source/alternate artwork repairs. | Meaningful covers survive reload and appear consistently in Home/Search/details; HTTP 200 alone is insufficient. |
| H02 | Validate | Accept deployed shared admission, artist relationship evidence and profile compatibility in fresh Recommended Artists. | Raw global popularity cannot bypass relevance checks; no name/title blocklists. |
| H03 | Validate | Verify deployed identity-owned portrait repair for artists derived from tracks. | Fresh artifacts gain actual artist portraits without album/track art substitution; inspect unresolved provider misses. |
| H04 | Fix / Validate | Validate deployed memory-only cookie loading, worker/API transport parity, safe failure diagnostics and bounded background recovery; trace any remaining source/scheduling failures and complete playable album details. | 95 focused checks and direct deployed-worker audio verification passed; master cookies unchanged. Fresh qualified inventory/four-card publication and open/play remain pending, without lowering coverage. |
| H05 | Validate | Accept deployed profile album frontier, exhausted-provider filtering, zero cursor wrap and persisted seed progress in fresh AWS enrichment. | Distinguish missing seeds, provider failure and verification failure; recommendations never become listening evidence. |
| H06 | Validate | Verify replacement of contaminated Popular Radio metadata in newly composed AWS artifacts. | No Unknown labels, duplicate physical sources or incompatible canonical/source merges. |
| H07 | Fix / Validate | Complete independent Popular Radio depth, discovery/mix supply and verified artwork. | 10–15 cards, >=8 playable tracks/card, >=3 non-mix discovery cards, >=2 verified mixes; radio does not block other rows. |
| H08 | Validate | Check radio novelty across rotations and artwork ownership. | At least three changed artist identities, new-first ordering, genuine owned portraits and distinct mix artwork sources. |
| H09 | Validate | Accept guest and account listening/count/history behavior after listening, repeat, reload and Retry. | Actual progression earns rows; seek/restored position does not fabricate plays; no guest-to-account leakage. |
| H10 | Validate | Accept deployed bounded Home foreground/re-entry revalidation. | Visible browsing stays stable, readiness stays backend-owned, and ready successors become discoverable. |
| H11 | Validate | Accept deployed Today's Pick cover and song/artist/play hierarchy. | Cover is actually attempted; missing-cover handling remains honest. |
| H12 | Validate | Accept deployed animated album hero, indicators/open/play and Featured Albums label once supply qualifies. | Qualified persisted supply, seven-second advance, reduced motion and responsive layout; usable details. |
| H13 | Validate | Accept deployed circular artists, distinct mix/radio cards and collages. | Desktop/iPhone presentation with genuine portrait/collage data; no fake zero-song counts. |
| H14 | Validate | Accept deployed recent/frequent strips, track actions, backend lanes/likes and collection details. | Preserve order, scoped likes/play/navigation and paging. Playlist writes remain L01–L05; assistant is W04. |

## Canonical Search

Evidence: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| S01 | Fix / Validate | Resolve cold first-attempt partial-title relevance and Top/Songs agreement. | Fresh when september ends must be assessed before private selection; retain ambiguity, uncommon names and version/credit guards. |
| S02 | Decide / Validate | Review stale snapshot serving and personalization quality in the existing Search pipeline. | Agree freshness and selection-memory behavior; verify existing revalidation instead of building a second search engine. |
| S03 | Fix | Expose recent search history and picked search play cards. | Account/guest-scoped history, clear behavior and correct replay/navigation. |
| S04 | Validate | Accept private rendition preference and cross-surface explicit-selection learning. | Home/artist/album/search selections teach only relevant explicit queries; auto-next/repeat does not; other accounts unchanged. |
| S05 | Validate | Verify newest-build Everlong enrichment, tab/detail return, revision replacement and paging. | No timeout disappearance, stale-generation results, polluted artist surfaces or scroll/pagination loss; correction opt-out works. |
| S06 | Fix / Validate | Measure and address cold/warm retrieval and response-construction latency. | Record first usable result separately from total enrichment and snapshot-hit time; no fixture-only latency claim. |

## Discovery queues

Evidence: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| D01 | Fix / Validate | Measure next-ten overlap, candidate/exclusion counts and exhaustion across new starts; refresh/expand existing related supply. | Shuffling alone does not pass diversity; transient provider failure is retryable. |
| D02 | Decide / Fix | Expose explicit new discovery/refresh action where needed. | Existing/restored active queue and album/playlist order remain stable; audio-cache eviction is not a recommendation fix. |

## Playback and failure traces

Evidence: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| P01 | Validate | Test warm/cold lock, tab/app switch or leaving the browser during startup on Brave iPhone. | Same-source recovery, honest errors and selected/audio/session agreement; browser termination is a separate limit. |
| P02 | Validate | Test Bluetooth and manual locked Play after immediate, 5-, 30- and 120-second pauses. | Compare command arrival, play Promise, media error and progression; verify pause/skip/foreground return. |
| P03 | Validate | Run 30–60+ minute queues with seek, repeat, next/previous, lock/unlock and deliberate failure/retry. | No silent advancing audio, false playing, wrong resumed track or unnecessary source re-resolution; retained diagnostics survive reload. |
| P04 | Validate | Retain complete same-session traces for fresh Everlong/Dream On/The Kill/Under Pressure-type failures. | UTC, selected/source IDs, operation/session, buffer/range/timing and exact rejection; lost historical logs cannot prove a cause. |
| P05 | Validate | Compare an Android/web 505 failure using exact sources, endpoints and local/R2 cache state. | Same title does not imply identical bytes; do not assume duplicate remote caches. |
| P06 | Fix / Validate | Profile queue wait, upstream prepare, first byte and decode/start for cold and fully cached cases. | Optimize only the evidenced stage; preserve bounded sequential warming, prepared-source reuse and byte validation. |

## Account and library

Evidence: [CURRENT_STATUS.md](CURRENT_STATUS.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| A01 | Validate | Finish email/password, Google, signup/recovery/expired-link, reload persistence and sign-out acceptance. | Ordinary login is user-observed; remaining flows need PC/iPhone coverage. |
| A02 | Validate | Verify cloud likes/playlists, like/unlike after reload and Android cloud refresh. | Correct user, track metadata and retained playlist order. |
| A03 | Validate | Switch between accounts and guest during requests and queue restoration. | No prior library/search/feed/settings/queue leakage; late async results stay scoped. |
| A04 | Validate | Play likes/playlists/albums and navigate all screens/details during playback. | Selected index/order retained and a single continuing audio source. |
| A05 | Fix / Validate | Fence Android local-like and cloud mutations to their initiating account. | library_catalog_provider currently re-reads active scope/user after await; account-switch device reproduction remains pending. |
| A06 | Fix / Validate | Reset/reload Android recommendation settings on auth change and fence older responses. | recommendation_preferences_provider currently retains an auth-independent notifier; correct account owns load/save. |

## Playlist editing and Android sync

Evidence: [CURRENT_STATUS.md](CURRENT_STATUS.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| L01 | Fix | Make concurrent same-playlist edits atomic or conflict-safe. | Preflight reads alone cannot prevent delete/insert races. |
| L02 | Validate | Exercise actual sync provider integration, offline edits, partial failure and retry. | No data loss or unintended remote-only deletion; helper tests alone are insufficient. |
| L03 | Fix / Validate | Build/install main-tree Android delta-sync changes and test two-device editing. | Remote-only playlists and order survive successive edits; changes are not assumed present in the web tree. |
| L04 | Fix / Validate | Complete web playlist editing controls and add/remove flows. | Targeted scoped writes and clear failure/conflict behavior. |
| L05 | Validate | Enable WEB_PLAYLIST_WRITES only after concurrency and two-device gates. | Default remains false; no gate enabled in this documentation pass. |

## Hosting and backups

Evidence: [OPERATIONS.md](OPERATIONS.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| O01 | Validate | Verify stable AWS public IP association and origin/callback behavior before stop/start or resizing. | Avoid unintended hostname, bundle-origin and Supabase callback breakage. |
| O02 | Validate | Measure a smaller-host fit under playback and worker concurrency. | 4 GiB is a test candidate; approximately 2.6 GiB observed use does not establish a 2 GiB fit. |
| O03 | Decide | Manage finite credit runway and pursue sustainable no-cost hosting. | The user registered neatie.duckdns.org; HTTPS retains the old web address and API origin. AWS is serving; Azure student signup failed for this user, Oracle A1 remains unavailable; offers are leads, not guaranteed entitlements. |
| O04 | Validate | Refresh and retain off-host backup after later code/runtime changes; bound retention. | Latest downloaded AWS archive predates history/Search fixes; on-VM daily backups alone do not protect against VM loss. |
| O05 | Validate | Test isolated full restore of the newer AWS archive, including configurations and service restart. | Hash/SQLite checks are complete; full restoration of this archive is not tested. External Supabase/R2 coverage is separate. |
| O06 | Decide | Determine whether September 21 onward Oracle runtime data needs recovery from the stopped disk. | AWS restored September 20 state. Preserve old data; no restart/deletion/migration is implied; ARM checks apply only to a chosen ARM move. |

## Remaining web product roadmap

Evidence: [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| W01 | Deferred | Port Charts/trending and its source/region/range/filter navigation. | Reuse existing backend; pair with G02 acceptance. |
| W02 | Deferred | Port New Releases and usable album-detail/playback navigation. | Android baseline was accepted; redo it only for a fresh reproducible defect. |
| W03 | Deferred | Expose lyrics and Behind the Lyrics with correct result/error states. | Reuse provider-bounded backend; interpretation validation remains G03. |
| W04 | Deferred | Expose recognition and assistant surfaces where appropriate. | Assistant re-audit was explicitly deferred; porting is not authorization for a broad redesign. |
| W05 | Deferred | Assess/port remaining advanced artist and library workflows. | Complete coverage/parity inventory against accepted Android behavior. |
| W06 | Deferred | Implement explicit account/cloud device handoff with ownership/conflict rules. | Local queue restoration is not cross-device continuity. |
| W07 | Deferred | Deliver installable PWA with update and cache lifecycle. | Validate iOS/browser constraints and updates. |
| W08 | Deferred | Design offline-download storage, eviction and playback. | Separate from PWA shell cache; validate device limits and scoped ownership. |

## Retained Android/backend gates

Evidence: [CURRENT_STATUS.md](CURRENT_STATUS.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| G01 | Validate | Run three distinct Home promotions and replenish ready depth to two. | No duplicate-job loops or immutable rewrites; backend alone promotes; fresh album/artist details work. |
| G02 | Validate | Check Charts provider/region/range correctness, full filtered lists, meaningful NEW markers and mini-player tap. | Preserve minimum playable publication and comparable prior-rank semantics. |
| G03 | Validate / Deferred | Check Behind the Lyrics interpretation/error handling and advanced artist coverage. | Separate provider absence from failures; preserve lyric-handling constraints. |
| G04 | Recheck / Validate | Verify Android managed native build, symbols and launch performance on device. | Current Gradle uses CMake/managedJniLibs; old jniLibs-transfer instructions are incomplete; profile skipped frames before blaming backend. |
| G05 | Validate / Deferred | Test signed split-APK self-update on actual devices and verify release configuration. | ABI selection, certificate, size/hash, permission and installer handoff; check production backend variable without publishing in this pass. |

## Tests, cleanup and documentation

Evidence: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

| ID | Status | Remaining work | Completion evidence / limits |
| --- | --- | --- | --- |
| T01 | Recheck | Reconcile catalog-coverage fixture expectation with playable-readiness contract. | Historical baseline failure is documented; correct test scope without weakening production gates. |
| T02 | Recheck | Re-run/triage the three recorded non-web Flutter failures when relevant. | personal_mix_detail_screen_test, recommendation_feed_models_test and track_menu_button_test; full-suite green was never established. |
| T03 | Recheck | Correct tests whose acceptance names overstate mocked composition or detail_ready evidence. | Retain structural tests; separately test real composition and persisted playable detail contracts. |
| T04 | Fix / Recheck | Add retained deterministic backend checks to CI/release gates. | Current inspected workflows lack pytest; Android release runs app_update_provider_test only; provider probes stay opt-in. |
| T05 | Deferred / Recheck | Review proven orphan production helpers, obsolete tests and intent-delta storage before deletion. | Old named Search helpers and intent-delta writer remain; recheck full dependencies, preserve unique assertions and active behavior. |
| T06 | Deferred | Consolidate repeated fixtures without deleting distinct cases; finish targeted semantic review as needed. | No blanket line-reduction target or claim that the whole branch was audited. |
| T07 | Deferred / Recheck | Review unused branding assets and production image/release packaging. | Confirm references/runtime imports before exclusion; tests remain in source. |
| T08 | Validate | Maintain the six consolidated guides and verify deployment/release documentation against actual workflow configuration. | Current branch/host/native guidance and mirrored cleanup evidence are corrected; future doc consistency and configuration verification remain. |

## Documentation and verification boundaries

Handoff is consolidated to six owning guides, with all 66 queue IDs retained. Unique implementation/test evidence is in CURRENT_STATUS, diagnostic/cleanup evidence in KNOWN_ISSUES, and rollback/backup/dated hosting research in OPERATIONS. Obsolete Oracle-live/untested-September-20-restore claims, travel/work naming and native transfer-only guidance are corrected in the current guides.

Historical test failures, cleanup candidates and provider offers remain explicitly dated and require rechecking. No application regression tests were rerun in this documentation pass. Current workflow configuration, device gates and ongoing guide consistency still require verification; T08 tracks that without inventing another backlog.

## Verification commands and constraints

Use the worktree owning the change. User runs flutter analyze; do not add it as an agent completion gate.
From the web worktree's app directory, use the cached SDK entry point to avoid Flutter's failing Git update:

```powershell
& C:/Users/ADMIN/flutter/bin/cache/dart-sdk/bin/dart.exe C:/Users/ADMIN/flutter/bin/cache/flutter_tools.snapshot test test/web_player_test.dart test/web_auth_test.dart test/web_library_test.dart test/catalog_api_test.dart --no-pub
& C:/Users/ADMIN/flutter/bin/cache/dart-sdk/bin/dart.exe C:/Users/ADMIN/flutter/bin/cache/flutter_tools.snapshot build web --no-pub --no-web-resources-cdn --dart-define-from-file=C:/Users/ADMIN/AppData/Local/Temp/neatie-web-public-config.json
```

The temporary config may need regeneration: ONLY public SUPABASE_URL and SUPABASE_ANON_KEY belong in a browser build, never backend .env/service-role secrets.
From web worktree root: python tools/test_web_playback.py after building; python -m pytest tools/test_web_preview.py -q. These use fixtures and do not prove live iPhone/provider behavior.
For backend changes, run focused existing search/snapshot, stream preparation, browser compatibility or related-track suites. For main-tree sync changes, run app/test/playlist_sync_delta_test.dart plus provider integration tests as added.

No commit/push/deploy is implied by a documentation-only task.
