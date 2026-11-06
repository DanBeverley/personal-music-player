# Architecture

**November 3–4 resource ownership:** inventory_codec v3 stores each exact item body once, with ordered pool memberships and their metadata. Distinct editions and playback proofs remain distinct. v1/v2 readers remain; v3 bounds canonical storage at 64 MiB and expanded logical data at 128 MiB, validates both hashes and restores independent mutable occurrences. Oversized enabled writes fail explicitly instead of silently writing raw JSON. Historical IDs/content hashes and logical schemas stay unchanged; older images require expanded recovery before rollback.

The maintenance tools may request `decode_inventory_payload(..., copy_references=False)` after the same complete checksum/bounds validation. This borrows reference trees for read-only validation, avoiding repeated copies while converting retained records and collecting retention dependencies. Application readers keep the default independent copies. Reference traversal visits a shared container once; this changes processing cost, not retention policy or the wire format.

ProfileRuntime now uses the existing bounded, expiring SessionStore alone. It serializes vectors/collaborative facts once, omits the global model, restores compatibility aliases and borrows only the exact matching resident model. Version mismatch is a cache miss; invalidation changes the profile cache generation. There is no second expanded-profile cache or new timer. CatalogDetail owns one accepted normalized detail projection; sliver builders retain desktop notes beside lazy songs. Library uses original filtered indices under its existing account/write guards. Native-only assets use platform selectors, not deletion or a second manifest pipeline.

Existing retention remains reference-aware, with a 256 MiB transaction payload bound alongside row/time limits; it skips rewriting unchanged dependency projections. Backup capacity checks preserve a reserve, and maintenance failures exit nonzero. Normal backups still use online SQLite copies; one-time writer-locked streaming recovery avoids impossible full staging. Root readers open SQLite sidecars as the database owner. One-time physical compaction compares all tables/values/schema and rejects a changed source before replacement; its original file is removed only after API health passes and a verified archive is pinned. Daily retention reuses freed database pages; this is not a new automatic daily VACUUM subsystem.

**November3 playback-handoff deployed:** browser initial stream URLs opt into direct delivery through the existing `_proxy_stream` cached owner. Only trusted signed create-only derivatives redirect; selected cached object/capability lives in the existing bounded session cache, preserving range identity. No mandatory pre-Play metadata request, no new endpoint. Canonical descriptors remain serializable without signatures; private capabilities stay ephemeral.

WebPlayer owns at most one speculative next-song stage via BrowserAudio's existing stage/commit/cancel contract, alongside backend next-five supply. Only verified cached next-track descriptors initiate browser preload; active audio keeps its resource. Natural ended and matching manual selection transfer the same stage into the existing selected-track flow. Account, track, queue, operation and stage revisions fence cancellation. Native playback events request completed-file status through the existing queue owner at most every15s; inflight/backoff still apply and no hidden timers keep a page alive. PlaybackPort separates assigned ownership from currentSrc matching: reconciliation accepts an owned resource still selecting, event filtering rejects old native events. Hidden native media failures get one immediate bounded retry; user/OS Pause and permission rejection are not overridden. CURRENT_STATUS owns tests/physical limitations; prior investigation descriptions below are historical.

**November3 deployed playback ownership:** MusicApi remains the single prepared-descriptor owner. Async catalog/queue replies are fenced by account and per-track descriptor/readiness identity; a healthy capability for the same canonical URI/MIME retains its URL until renewal is needed. WebPlayer fills the next-five preparation horizon with two concurrent tasks; known previous sources follow. Native progress/foreground events refill it without hidden-tab timer dependence. Full caching is rolling two-worker work with bounded10-item client demand; first-five preferred readiness/status is separate from full-file count. Backend alternate extractors reuse healthy client/credential-scoped setup within existing pool bounds. Direct cached delivery is enabled; canonical path/version guards, expiry and legacy relay prefix preparation remain. No new player/executor/cache subsystem.

Diagnostics retain one owner with exact incremental compact/pretty byte accounting and isolated mutable snapshots. Existing listening_room owns the shared seek slider used by both players: local drag preview, one release commit, track/epoch fence. CatalogApi retains at most eight ready, account-scoped five-minute snapshots and coalesces in-flight shelf calls; retry bypasses cache. CURRENT_STATUS owns measured improvements and physical acceptance; older disabled-direct labels below are historical.

**November2 deployed ownership:** BrowseWidgets owns artwork-backed surfaces/date formatting; GuestHome bypasses personalized Home, and CatalogScreen retains playable suggestions/likes placement. Dock and ListeningRoom retain the same audio/layout owner; native/web charts share RankHistoryPainter. LibraryScreen owns one song-destination picker used through an optional callback by existing song surfaces. WebLibrary remains the sole browser cloud-write owner, now retaining created_at and returning the created ID. Existing SQL create can enter its append branch within one transaction; no second endpoint/table/store. Optional first-song acknowledgement is checked before publishing success. Old-scope picker choices and stale queue callbacks cannot write to the new account. Song-chat tone/cache version5 stays in existing media_runtime; research/citations/model are unchanged. Main Android delta sync differs from the preserved older native copy here; do not consolidate by importing native filesystem/state providers into web. CURRENT_STATUS owns deployed checks. QueueScreen now owns only the embedded list/gestures; ListeningRoom owns search, cover/list presentation and lyrics/chat. Its duplicate standalone controls are removed; active QueueCoverBrowser remains.

**November1 readiness/Home growth is deployed:** the ownership and bounds described below now serve with matching client/backend. Metadata-only descriptors do not establish startup-byte readiness; recent-artist acquisition demand uses existing durable progress/worker inventory. No new subsystem/schema/job type. Exact-image/public checks pass; physical iPhone and actual Home supply/promotion acceptance remain open. CURRENT_STATUS owns hashes and limits; older local-only labels below are historical.

**November1 local readiness/Home growth:** the existing MusicApi descriptor cache owns immutable file/type identity; its preparation-expiry marker separately tracks completed byte preparation. Metadata-only descriptors do not suppress real next-track warming. The existing AAC index owner computes bounded startup extent; stream_buffers retains128KiB normally/up to256KiB under unchanged32MiB/128-entry limits, with ETag/length checks. Native Play remains gesture-bound. No second player/cache/executor.

Home enrichment uses recent listening and durable catalog/artist-graph/album progress as coverage demand independent of total stock. Seed selection interleaves recent/familiar before its bound. CandidateSupplyService bypasses the fresh-stock shortcut only when this demand exists, retains additive verified inventory and reports remaining demand through existing worker follow-ups; remote work stays outside customer feed responses. Promotion, radio, playability and provider budgets retain existing owners. Local only; CURRENT_STATUS owns checks/limits.

**October31 rollout:** the AAC/chat/Top ownership described below is now deployed in song-conversations, including the prior catalog/Search-focus candidate. No additional architecture is introduced. New Home investigation finds quantity-based acquisition gating lacks newly listened artist coverage demand; existing additive inventory and bounded workers are to be retained. CURRENT_STATUS owns generated evidence, not user-profile confirmation.

**Local October31 AAC/chat/Top batch:** preferred_cached_variant is passed into the existing bounded stream preparation worker. Cached representation selection precedes byte warming; its proven prefix is reused, and cold misses skip optional codec probes. One shared repair predicate accepts valid AAC and Opus; the post-preparation codec overwrite is retired. ListeningRoom owns a stable vinyl/queue layout plus a separately animated/offstage companion. SongDiscussion owns bounded per-song history through the existing scoped BrowserSessionStore; one widget retains transitions, and only eight recent messages are sent to the stateless API. No server conversation table or new player/cache. Existing search.pipeline normalizes provider Top identities before ranking/snapshot selection: VL containers remain playlists, nested artists expose their proper identity. provider-search-v2 namespaces the corrected snapshot contract. Existing entity/surface inventory, paging and ranking owners remain; CURRENT_STATUS owns limits/checks.

**Local October31 Search interaction:** PlayerPage owns the Search FocusNode and a navigation revision. CatalogScreen observes activation/reselection, exits detail and returns its existing scroll controller/input to the top without clearing query state. Native input reattaches on the next frame after scrolling/detachment; mounted/active/detail/revision guards prevent stale focus after leaving Search. The suffix painter morphs arrow-to-X with reduced-motion support; clear uses existing query cancellation, not a new Search path. No backend/player/cache owner changes.

**Local October 30 catalog-editorial candidate (not deployed):** `app/lib/web/browse_widgets.dart` owns paper/ink and panoramic headers, responsive song rows, typography and supplied release-date formatting. The old record painter is retired. CatalogScreen retains Search/state/paging while arranging the lead beside songs on desktop; CatalogDetail retains loading/playback and presents existing related-artist/biography data and album liner notes. LibraryScreen retains account/write/revision guards and collection navigation, with numbered cover jackets and saved-song previews. Separate page-storage keys/reduced-motion entry remain; repository changes clear selection. Collages reuse HomeArtwork/ArtworkImage; accessible labels stay inside InkWell. No new fetcher, metadata lookup, Search engine, image cache, player, store or backend path. CURRENT_STATUS owns checks.

## Reliability cleanup — deployed October 30

`api/stream_core_runtime.py` owns preparation/delivery and reexports existing entrypoints. `stream_sources.py` owns acquisition, persisted leases/failures and generic sessions; `stream_buffers.py` owns prefixes, initial response handles and representation ranges; `stream_cached_audio.py` owns stored-audio validation/repair. Each lock/ContextVar has one owner. Prefix waiting has one25s monotonic budget. Preparation has one85s response budget, nonjoining shutdown and eight global slots released only when actual work finishes. Emitted unpinned bytes cannot be followed by a refreshed unproven representation or a successful short EOF.

`home_feed/repositories.py` retains repository construction, Home state and promotion; `repository_jobs.py` owns durable admission/leases/retry; `repository_records.py` owns schema, immutable inventories/artifacts and exposures. Common codec reads, usable-snapshot queries, artifact decoding and job SQL have one implementation. Existing account/revision/lease/immutability contracts remain. Five files are added to establish these boundaries; no second player, source cache or job system is introduced. CURRENT_STATUS owns exact-image checks and before/after timings.

## Cold-start isolation — deployed October 30

Home API successor checks enqueue a small durable `reconcile_supply` job. The existing candidates worker lane runs the relocated inventory decoding, freshness, signal, reserve and successor checks, serialized with candidate/radio expansion by the existing inventory semaphore. Jobs coalesce by durable scope/state/window/intent; explicit refresh can expedite pending work, and execution reads current state. No full inventory is loaded by this API scheduling path. Promotion authority and composition contracts remain unchanged. Leases/retries stay in HomeJobRepository; no additional worker process or provider pipeline is introduced. Rollback must archive/retire the new job type before old enum readers restart; OPERATIONS owns that procedure.

Extraction preserves the resolution ID before each fresh attempt. Fixed numeric solver counters distinguish prepared/raw player input and calls; existing stage wall timing and new caller-thread Python CPU timing are separate. Caller CPU is not Node CPU. CURRENT_STATUS owns controlled/native/live timings and remaining iPhone acceptance.

## Cached-audio direct transport — included, disabled

The prepared-source owner optionally includes a signed R2 delivery capability alongside the unchanged canonical playback path, version, MIME, duration and expiry. Only validated AAC/Opus derivatives qualify: their sole publisher uses create-only conditional PUTs and no deletion/replacement owner exists. Mutable legacy audio keeps the ETag-checked relay. No duplicate audio store, new player or broad external-URL acceptance is introduced. Backend flag `NEATIE_DIRECT_CACHED_AUDIO` and frontend exact `NEATIE_AUDIO_STORAGE_ORIGIN` are explicit opt-ins; no rollout yet.

MusicApi validates the canonical descriptor first, then exact configured HTTPS origin, source/variant/version binding and signed expiry. Native URI mapping uses the capability only while sufficiently valid; persisted state contains canonical URLs only. The existing metadata lane renews expiring capabilities, while the relay remains immediately usable. WebPlayer retains the actual delivery expiry for resume; BrowserAudio retains native ownership and emits safe identity/route fields without signature URLs. Direct descriptors skip API prefix warming. Cold sources continue through the existing acquisition/validation/streaming owner. CURRENT_STATUS owns local benchmark and device limits.

## Song conversation ownership — deployed October 30

`SongDiscussion` lives inside `ListeningRoom`; it never creates/owns an audio source. Explicit compact Play/Pause controls use WebPlayer. Opening chat replaces the phone's tall player stack; desktop retains vinyl beside the full-height conversation. User questions call canonical `/assistant/chat` with bounded song context/recent turns. `AssistantService` delegates song mode to the existing meaning owner for one synthesis and bypasses saved sessions/vector memory/general planning. Chat requests only `reply` and cited `source_ids`; the complete reply becomes the existing API response. Native `/lyrics/{id}/meaning` retains its report schema through the same owner. Lyrics come from supplied lines or a cache-only `LyricsService` read; no second lyrics-provider chain.

`lyrics/context.py` owns bounded public research. Song identity and a question-topic query go to existing DDGS, without private question/history. A fixed-host Wikipedia API obtains artist-matched reference passages; ordinary chronology uses a successful reference directly. Other questions can read at most two search-result pages. Every arbitrary page/redirect is checked for public addresses, pinned to that IP with original TLS hostname/SNI, and bounded by bytes/time. Search leads and read page excerpts remain distinct; reading a page is not independent fact verification. Public topic context max64 entries,30min with read pages/90s with snippets only. Chat research is automatic. Prompt separates narrative/interpretation/documented claims, distinguishes writing/recording/release, supplies current UTC date and requests reconsideration on retry. Only valid returned source IDs become links. Existing response-cache keys include mode/date/model/evidence/history/scope. The existing single readiness log adds numeric research time/pages, never prompts/lyrics/answers.

AI identity is derived server-side: verify signed-in Supabase Bearer tokens or derive a guest scope from a separate32-byte private capability. Caller scope IDs alone grant no access. Stateless anonymous interpretation cannot access saved sessions. Existing session CRUD remains scoped, and missing requested sessions return404. Two inference slots, per-scope/global minute bounds, request/history/lyrics limits, one model attempt and optional three-result DDGS lookup bound cost. No model key goes to Flutter. Live protected configuration uses direct Ollama Cloud/Gemma4:31b; cloud JSON is requested in the prompt and parsed/validated without unsupported native structured-output parameters. Local Ollama retains its existing schema format.

Successful interpretations have bounded30min RAM caching keyed by scope, question, metadata, lyrics, history, evidence and model. Model failures/empty answers are errors, never substitute interpretations. Panel retains eight messages/10k transmitted history characters and3.6k lyric characters. Composer stays editable during work; a pending submitted question is separate from the next draft, and completion/cancellation cannot overwrite that draft. Identity changes discard old replies. Source chips have separate accessible actions. Arrival/view animations are bounded; typing rebuilds the composer and ordinary position ticks do not rebuild the conversation. Reduced motion jumps scrolling immediately and uses a static pending indicator. Replies are complete-message transitions, not streaming. Client cancellation does not guarantee provider work stops; its timeout remains. CURRENT_STATUS owns release/live checks and remaining device/quality limits; older local-only entries are historical.

## Generic session maintenance

October29 backend `session-refresh` extends the existing single startup owner rather than adding a scheduler/executor. Existing idle policy gates all leases/network work; the30s check refreshes only near the original context expiry (live900s,60s margin). Provider failures back off to the TTL, capacity contention skips without poisoning a source, and old valid configuration remains until verified replacement. Cookie scopes, two bounded generic contexts, byte caps and per-track fresh tokens/responses are retained. Generic work continues through existing extraction admission, so foreground playback never waits on the maintenance owner. CURRENT_STATUS owns expiry-boundary benchmarks and remaining browser/provider latency.

## Current artwork and playback ownership

October29 release keeps the existing Search, artwork and playback owners. Search profiles come from authoritative typed artist/Top shelves; contributor credits remain on tracks. Metadata batches continue under bounded, version-fenced ownership without client polling. Each refresh and metadata batch uses at most two of the four provider slots, preserving all shelves and the existing deadline. Artwork has eight cached-read I/O slots, two conversion slots and16 shared-work admissions; publication prepares three display sizes from one validated download. Storage faults retain source eligibility, and Home retries respect actual cooldown without spending attempts while blocked.

With `NEATIE_AAC_PLAYBACK=1`, the existing warm queue creates demanded immutable `aac-v1` M4A resources alongside Opus. One conversion slot/eight queued repairs; no initial-Play conversion wait or extra optional HEAD. Preparation and cached metadata carry the preferred variant, validated duration and exact resource version. Existing valid media stays immediately usable while background hints discover upgrades; active streams never switch representation. iOS preference is AAC when ready; desktop retains normal selection, with URL comparison overrides. Cold publication can schedule remembered AAC demand. No new cache/player subsystem. CURRENT_STATUS owns measured results and physical-device limits.

## October29 root-cause audit — proposed corrections, not implemented

Artist profile acquisition must carry authoritative entity type/ID/portrait together; track credits/video uploader IDs are not sufficient profile proof. Canonical Search currently fetches three shelves and reconstructs artists, causing avoidable per-ID enrichment. Its bounded metadata owner should finish admitted work independently of eight client polls, retaining resource/idle limits. Artwork delivery must separate cached object I/O from limited image conversion and coalesce identical variant work. Download/source validation and R2 read/write outcomes need distinct retry eligibility; a storage failure must not suppress a valid source for six hours. Existing owners can implement these corrections without another engine/cache subsystem. Current staged readiness waits on native canplay; format/layout compatibility must be measured using metadata/readiness/actual output, not a moving clock alone. Existing AAC rechunking offers a tested layout candidate, not a new approved delivery policy. CURRENT_STATUS owns proof.

## October28 local corrections — not serving

Normal/staged BrowserAudio now share typed-source assignment/error ownership, preserve the candidate element/type on adoption and retain real native timing boundaries. MusicApi coalesces preparation by track/full-cache intent within account scope, reuses valid descriptors and removes superseded hint work; first Play still does not await metadata HTTP. Canonical Search gives unique exact artist evidence precedence except exact song-title collisions/ambiguous artists; empty snapshots have a short freshness window under existing coalesced refresh. Queue picker observes bounded revisions and pages the same inventory. Home respects current readiness before timeout; dialog/queue receive a stable account-aware library revision signal. Shared/native image attempts finish finitely and retry only on new intent; native callbacks are generation-fenced. Worker heartbeat uses one transaction and contains only bounded busy contention. Sparse correlation adds no per-frame/per-progress traffic. CURRENT_STATUS owns local proof and remaining device limits; prior proposed-correction wording is historical.

## October28 investigation corrections to existing owners

The deployed staged candidate does not yet share normal typed-source assignment/error ownership; adoption loses type and real Play-call timing. Repair this owner rather than adding another player. Home readiness must be checked before applying a wait-specific UI deadline; backend retains promotion authority. Dialog routes must observe account-scoped WebLibrary directly for optimistic likes/rollback. Queue Search uses canonical /search but currently ignores pending/revision updates and truncates six rows. Fix negative-result freshness and picker observation there; exact-artist Top selection belongs in SearchService's single entity decision. Shared/native artwork loaders need finite final-source outcomes while preserving identity checks. Recommendation heartbeat is operational bookkeeping; transient SQLite contention must not escape and terminate all worker lanes. These are proposed corrections, not implemented contracts; CURRENT_STATUS owns reproductions.

## Staged playback deployed / song conversation planned — October28

Web `2026-10-28-staged-handoff` now serves the staged playback and minor-action contracts below. Backend remains playback-recovery; AI conversation is still investigation/planning. CURRENT_STATUS/OPERATIONS own hashes, public checks and rollback; older local-only wording is historical.

`PlaybackPort` adds optional stage/commit/cancel; existing non-browser consumers retain default unsupported behavior. BrowserAudio owns one active Media Session plus at most one preloaded candidate. It observes candidate readiness privately, pauses A only at B's ready commit boundary, requires native Play success, adopts B without reload, moves existing listeners and releases A. Rejection restores A; explicit cancellation cannot resurrect it. Candidate events do not change active metadata/history. Bounds are90 seconds preload/3 seconds commit and existing32 resource attempts. This is not WebKit acceptance or a hidden keepalive.

WebPlayer keeps A's queue/identity/time until commit; pending selection has separate UI status and operation fencing. Queue replacement, listening epoch and B history happen after success. Rapid selection, Pause, account change, disposal and removal cancel the candidate; reorder retains its occurrence. Deferred incumbent errors are reconciled on failure, ended incumbents are reported without auto-next over the user's selection. One source-selection helper retains existing verified URI/MIME/lease/retry rules for both paths. First/paused/recovery starts remain gesture-first.

Song AI should reuse existing lookup helpers/source links and chat ownership: bounded explicit lyrics/song context → optional factual lookup → one synthesis → scoped follow-up turns. Existing vector memory is conversation memory, not external song evidence. No mandatory interpretation/planner chain, arbitrary URL fetch or new RAG database is justified by current scope. CURRENT_STATUS owns unimplemented inference/auth/failure/lookup prerequisites.

## Local minor-action ownership and AI audit — October28

Queue-search row playback and plus-only addition share the existing queue/player; playlist-add consumers retain add behavior. Likes use per-track acknowledged state plus pending revisions inside the serialized cloud-write owner. Refreshes protect changes newer than their read, with account/disposal guards. Same-account token rotation is allowed; requests retain their captured token. Dock artwork uses an opaque playback-operation key to retry exhausted same-URL sources; bounded aggregate outcome/no-source fields survive export without URLs.

Existing AI owners are distinct: media runtime performs structured lyric interpretation; assistant runtime handles chat/sessions/tools. Meaning has no question/history; chat requires caller-supplied turns and lacks explicit song grounding. Future vinyl conversation should reuse chat with bounded song/lyrics context, without a mandatory summary call. Server-verified scope, working inference and truthful unavailable/cache behavior precede exposure. This audit adds no AI implementation; CURRENT_STATUS owns evidence.

## Current playback recovery ownership — deployed October28

`2026-10-27-playback-recovery` retains the existing player/transport owners. Failed resources and watchdog-confirmed no-progress timeouts may select a validated same-track immutable descriptor learned after startup. Healthy playback/raw advancing stalls do not switch; two automatic attempts include deferred foreground dispatch, with at most one immediate hidden cached replacement. Pause/permission/scope/operation guards remain authoritative. Seek assignment is pending until native completion/readiness and is not repeatedly restarted.

`_AudioStreamingResponse` owns both suspended async iteration and already-open transport bodies, covering header failure before first iteration. The measured wrapper closes its inner iterator; R2/YouTube/Audius resources release through the existing byte-worker lane. Cleanup preserves the original active failure. Existing synchronous reads remain governed by existing timeouts; no concurrent close of a running generator, codec change or range-guard relaxation. Sparse buffered-ahead/seekable/pending target/choice evidence is exported. Earlier investigation-only statements below describe pre-fix checkpoints; CURRENT_STATUS owns tests and limitations.

## Playback investigation contract — October27 follow-up

Origin/API write completion, public-proxy accounted bytes and native audio progress are separate measurements. A deliberately cancelled client can yield a partial proxy record/duration0 after full origin write. Preserve all boundaries; never infer phone receipt or closure initiator from one. Real-format partial delivery/hidden recovery must supplement immediate-WAV desktop checks.

Existing recovery currently ignores immutable cache descriptors learned after startup; artifact regression proves that branch. A correction belongs to `WebPlayer` source-selection/recovery ownership with exact track, scope, operation, saved-position and Pause guards; do not swap healthy sources or create another retry owner. Existing initial cached response binds its session; same-session nonzero ranges pass and restart protection must remain. No application behavior changed in this investigation; CURRENT_STATUS owns traces/tests.

## Current discovery/delivery ownership — deployed October 27

`2026-10-27-discovery-delivery` includes the previously local session-quality contracts below. Existing acquisition owners now request artist-graph work for album shortages, preserve relationship edges separately from recording results, and inspect a bounded48-seed frontier before applying the16-eligible execution cap. Existing job progress carries edges; current-profile relationship validation still gates their use. Provider concurrency/work limits and proof requirements are unchanged. `_shortlist` funds familiar/discovery whole-album groups within the same16-proof budget; an artist edge or canonical ID is still not playability evidence. Customer feed composition remains persisted-only.

Delivery distinguishes final-response completion, sent-byte completion and disconnect timing. A normal disconnect after successful final body remains complete; API-to-proxy completion does not establish phone receipt/audibility. Browser evidence stores up to12 completed transfer entries per play, each with validated correlation IDs/timings, plus source-mutation/pause/native evidence. Unfinished transfers can be absent from ResourceTiming; zero bytes/status0 is not failure proof. Foreground/remote Play restores retained metadata only when missing; no periodic paused keepalive, invented playback or override of intentional Pause. Desktop pause proof is not iOS acceptance.

CURRENT_STATUS/OPERATIONS own current release verification and rollback; older investigation/local-only wording below describes earlier checkpoints.

## Pre-implementation investigation: acquisition is not qualification

Pending album metadata, relationship evidence, verified playable tracks and admitted feed cards are separate stages. Latest follow-up finds suitable new-artist albums waiting for proof while missing-count-first verification favors easier familiar albums. Future correction belongs in existing proof funding/planning owners, preserving complete album thresholds and durable progress. Album deficits must be able to request relationship discovery even when artist/track rows are full; retain relationship metadata independently of whether another provider currently returns recordings. Move execution bounds after durable cooldown/cache inspection rather than adding another acquisition path.

Delivery `transferComplete` describes API-to-proxy bytes, not audibility or client receipt. A later disconnect cannot by itself turn a complete response into a failed stream. Desktop pagehide/Media Session controls do not prove native iOS lock-screen ownership. These are investigation findings; source fixes are not applied in this pass.

## Session-quality candidate ownership — not deployed

Media Session describes requested native playback, while advancing media time remains the proof of progress. Intentional Pause still wins. Diagnostic frame callbacks only update fixed histograms during bounded foreground windows; no audio sampling, logging or storage per frame. Actual Flutter frame timings and RAF callback intervals must remain separately labeled. Image counters aggregate completions; complete IndexedDB archives remain independent of bounded summaries and replay yields between batches.

History counts retain their existing identity/event semantics. Exact entity owners and source aliases use one indexed batch query; pure text cleanup does not bind/copy the server. Existing history requests expose server processing/route-entry and database/count timings, with browser decode measured separately; route-entry time is not proof of thread-pool waiting alone.

Queue ranking preserves all eligible candidates, with soft exposure balance and greedy artist/album variety; provider order is a bounded tie-breaker. Bounded artist-owned, explicitly playable persisted album works supplement the same queue owner without new network acquisition. Home balances already-admitted familiar/discovery groups without comparing incompatible acquisition score scales; exposure recency and cross-shelf reuse constraints remain. No admission/promotion/source-proof subsystem is replaced. Legacy source repair requires matching provider thumbnail identity; credit fallback excludes synthetic Unknown Artist but preserves Various Artists.

## October 27 resource ownership

Worker orchestration reads `SnapshotReference` metadata; only the consuming supplier/composer expands and validates inventory bodies. Scope/profile/time ordering matches full snapshot reads, without retaining a duplicate tree through composition. Non-progressing incomplete radio work keeps its durable job and waits 5/10/20 minutes, capped at 30 minutes; interactive priority can expedite it. API startup initializes a missing idle marker atomically without resetting existing last-use time. Actual request classification remains the authority for waking optional work. These changes do not change admission, freshness or promotion contracts.

## October 26 live ownership contract

Serving `2026-10-26-history-radio-recovery` includes the H09/P04/H07 contract below. Correlated terminal provider failures may arrive before native Play rejects; WebPlayer must preserve that reason, fence late startup/recovery completions and avoid another source attempt. Transient errors retain bounded recovery. Current-track/source ownership remains required; accessibility semantics expose the same error as the expanded player.

Candidate/radio jobs remain singleton per account/type across idle generations. Identity-specific artwork jobs remain separate. Startup reconciliation cancels only superseded waiting supply work, preserving running leases and immutable job history. Existing lease recovery owns expiry; reconciliation must not replace its clock/ownership rules. Radio logs expose aggregate measured stages without introducing provider calls into feed response paths.

The approved live Supabase migration installs revision/conflict checks, transactional edit functions, legacy-write triggers and RLS. Application rollback retains this additive schema. Three playlists/30 tracks were preserved; real-device concurrency remains an acceptance gate. CURRENT_STATUS/OPERATIONS own deployment evidence; older local-only headings are historical.

## October 26 H09/P04/H07 contract — deployed

Historical counts belong to committed play events, independent of recommendation suppression. Latest exact-source metadata and owned catalog/recording aliases resolve one counting identity; names alone cannot merge recordings. Web play keys remain idempotent and legacy import deduplication remains bounded to its existing window. Derived history v3 rebuilds incompatible computed snapshots without deleting raw events. Browser pending overlays join authoritative exact-source/recording evidence until matching acknowledgements retire them.

Manual silence is captured once by the shared diagnostic event owner, including archive replay; the UI invokes recording only. Observed transfer counts survive later unavailable samples inside one trace, never leaking into another load. Complete archives and bounded UI summaries remain separate.

Radio plans retain the admitted artist frontier. Durable cache/backoff inspection precedes the existing four-request execution limit; provider backoffs/one-MusicBrainz cap and persisted source qualification remain. Profile-aware zero discovery is authoritative. Aggregate refresh funnels describe measured stages even on early/no-work returns and do not mint new supply IDs or move provider work into feed responses. Natural refill/rotation/device acceptance remains distinct from local contracts; CURRENT_STATUS owns tests and serving state.

## October 26 serving contract

The acquisition/preparation contract below is deployed in backend `2026-10-26-acquisition-prepare`; existing identity/proof/admission, compact-storage and backend-only promotion authority remain. Web `2026-10-26-media-artwork` injects the existing MusicApi track-artwork selector into the persistent BrowserAudio owner. Media Session text publishes immediately; image loading/fallback is optional asynchronous work with a 1.5-second candidate deadline and track/generation/disposal guards. Enrichment reuses the latest metadata text and retains a still-valid current cover. Artwork diagnostics contain safe outcomes/counts/timing; no audio source replacement, image service, new API or UI redesign is introduced. Physical native-card acceptance remains distinct from compiled browser proof. Control/audio origins and enabled cloud-playlist editing are preserved.

## October 26 acquisition/preparation — local ownership contract

Candidate evidence preserves artist-owned YouTube IDs from existing artist fields and matching credits. Unique identity evidence can enrich a stronger name-only graph edge; conflicting owned IDs or canonical identities remain separate. The existing planner passes this identity to the artist-ID catalog resolver and keeps original page/proof bounds. Provider failures retain specific safe identity error codes. Real source-proof progress advances the existing bounded candidate job with a 30-second continuation even before album admission; `ready`, usable-content `made_progress`, immutable generation identity and metadata/proof distinction remain unchanged.

Preparation returns availability metadata and reuses the existing post-response/coalesced successor owner; no full-feed projection or waiting for inventory expansion. Promotion exposure belongs solely to the existing impression/exposure repository, including visible Popular Radio identities; it does not mutate a radio inventory blob. State reconciliation caches only 64 exact-payload SHA-256/accepted results, never payload trees. Scope and time checks precede each cache use; altered payload bytes require full validation. Direct-history IDs reuse the existing composition-only context and never persist across profiles/compositions. No new queue, table, search engine or artifact quality state. CURRENT_STATUS owns measured local acceptance; serving stays refresh-demand.

## October 25 verification correction — local contract

`stream_core_runtime.BACKGROUND_EXTRACTION_CAPACITY` is the shared source of the existing background semaphore/verifier resolution limit. Source metadata workers remain concurrent. Admission-busy outcomes leave source rows/provider health/failure counters unchanged and stop the remaining batch; legacy busy retry clocks do not block recovery. Renewal admission retains unexpired proofs without extending them. Candidate supply defers through the existing bounded durable retry with 30 seconds, preserving no-progress accounting and avoiding repeated cycles in the same refresh.

Album shortlist funds minimum coverage gaps before optional extras, sharing planned canonical keys across albums. Stored pending candidates and lookup results are merged in shortlist order before the resolution budget is applied. Identity/version/coverage requirements, foreground slots, pool reserves and backend-only feed promotion remain unchanged. No new acquisition subsystem, persisted quality state or schema. Implemented locally with regression/isolated-provider proof; production remains refresh-demand. CURRENT_STATUS owns acceptance limits.

## October 24 source verification — measured contract mismatch

The source-registry verifier currently permits four workers, but the stream extractor reserves only one background slot. Busy admission is not evidence against a source: core stream failure caching already excludes it, whereas `_verify_source` currently persists a 900-second transient block. The measured repair belongs in these existing owners: keep metadata matching concurrent, coordinate resolution with available background capacity and defer busy work without poisoning source/provider state. `_shortlist` must fund album minimum proof deficits before extras instead of flattening entire unresolved groups. These are confirmed repair requirements, not implemented behavior; identity, coverage, foreground reserve and sole backend promotion authority remain unchanged. CURRENT_STATUS owns configured isolated and production evidence.

## October 24 candidate reach/composition correction — local contract

Artist-ID resolution accepts exact or nonempty standard-normalized names only when the distinct provider ID is unique. The existing 160-entry pending metadata owner now selects related unheard artists before familiar/unsupported catalogs and rotates album slots per artist; consolidation and refresh share the selector. Partial metadata remains complete and false-playable until the source verifier's original coverage requirement passes. No additional inventory owner or quality state is introduced.

The candidate worker owns strongest canonical artist-edge selection and recovery into existing album/pending metadata. Presentation exposure influences reserve/priority; it does not exclude album acquisition. Relationship eligibility keys use the existing Unicode-preserving catalog normalizer in both worker and planner, retaining provider progress/cache keys. Source verifier still owns proof admission. Relationship-only album changes enter the existing inventory content hash and publish through the existing immutable revision/CAS path; no schema, store, engine or quality state is added.

Production-default album allocation balances eligible familiar/unheard artists, avoids cross-shelf artist reuse where alternatives exist and retains one/two artist limits. Catalog enumeration order has no relevance weight. Successor novelty averages visible-row changes, measuring album identities and artist changes separately; mix depth and removed rows cannot inflate it. The old global identity threshold and experimental-only album-ranking branches are retired. Parent-row retention carries its original deadline. Backend remains the sole promotion authority; frontend and Popular Radio contracts are unchanged. CURRENT_STATUS owns local tests/replay and serving distinction.

## October 24 refresh-demand serving contract

`2026-10-24-refresh-demand`: interactive candidate/radio demand reprioritizes the same durable singleton (110), preserving a live lease/input/attempts. Pending signals/due time update; bounded allowance extends only on priority promotion. Provider retry/circuit clocks remain authoritative. Idle priority 30 clears completed UI intent. Forced consolidation continues into the existing six-request/16-proof owner; verification prioritizes profile-related albums before unrelated retained metadata with unchanged proof/admission thresholds. Track refresh scoring now rests recent exposures using the existing seven-day album age policy; soft familiar/new balance and shallow supply remain. No new acquisition engine, quality state, table or provider work on response paths.

Rejected composition is terminal failed work with a reason, distinct from a stored artifact. Queue readiness reports blocked composition separately from background supply and ready depth independently of the two-slot target. Current rows survive; client wait is bounded at 90 seconds with a truthful message. Backend promotion/version/token authority remains unchanged. Trial stays off. Two complete, novelty-accepted successors are stored; actual device promotion/open/play remain acceptance gates. Earlier serving sections are historical.

## October 24 serving contract

`2026-10-24-album-catalog` deploys the replacement and pending manual-refresh completion described below, retaining artifact/codec identifiers and immutable active/ready/fallback dependencies. Composition fact reuse is active; `NEATIE_HOME_DISCOVERY_TRIAL` stays off. Acquisition remains bounded background work; deployment does not retroactively change retained artifacts. Two independent local Search benchmark edits are excluded from this source-layer release.

## October 24 album replacement (pre-release design)

`enrichment.py` retains sole acquisition ownership. YTMusic `album_catalog` v2 progress/page keys retire name-search caches. Resolve a unique artist entity, retain its ID, browse the album section, cache bounded listing facts separately from stable album details, and prepare three entries per page. Cursor advances over non-preparable/single entries; empty accepted output is not catalog exhaustion. Revisit catalog heads after six hours; 512-card resource bounds retain incomplete-catalog diagnostics. Duplicate results remain cached; unchanged metadata does not trigger repeated provider fetches. MusicBrainz canonical artist-ID pages and reconciliation remain in the same pipeline. Identity ambiguity is entity-specific, not a provider-wide outage.

Album relationship origin, actual detail credits and catalog owner IDs persist. Assessed evidence controls discovery seed expansion; unsupported historical name-only lists no longer drive product acquisition. Base album proof admission is authoritative. Segments/freshness are rebuilt projections; a bounded `album_verification_backlog` in the existing mutable acquisition ledger preserves complete metadata between 16-track verification batches. It is not a feed/artifact quality state or another storage owner. Source renewal starts within 24 hours of proof expiry, respects retry/circuit/budget rules and cannot extend proof validity on failure. Canonical coverage/alias counting and profile-qualified artist counts drive existing reserve targets. Current/ready/fallback immutable feeds and codec/model contracts remain retained; no deployment yet.

## October 23 acquisition audit — current contracts and defects

CandidateSupplyService consolidates persisted inventory, plans background provider work (six scheduled requests), materializes/cache-coalesces results, verifies at most 16 new track sources, builds/canonicalizes pools, consumes cached release metadata, assesses reserve and persists candidate/detail payloads. Customer feed response consumes persisted supply. Albums need complete coverage when below six tracks; otherwise at least six and 80%. Album/artist metadata is not stream proof.

YTMusic album acquisition currently name-searches albums and takes three matching entries; no catalog continuation is consumed. MusicBrainz artist-ID pages use offsets and a 90-day canonical result cache; YTMusic results use 14 days. Evidence is flattened into priority artist strings and album output does not inherit request relationship fields. General inventory merging retains segment pools independently, while consolidation revalidates only the base album pool: derived views can outlive their current proofs. Working reserve can therefore report healthy while actual proof-current/profile-qualified alternatives are scarce. Fix these existing owners/views; do not add a second acquisition engine or remove relevance/identity/source/detail contracts. See CURRENT_STATUS for measured funnel and proposed order; no fix is deployed.

## October 23 controlled Home candidate (local only)

Modern explicit refresh sends `refresh_from_version` and an idempotent token. With no successor, the API returns active immediately with `refresh_preparing`, using the existing post-response replenishment owner. The client follows ready-count/job status even with populated rows; cancellation/account/generation fences reject late results. Completion bypasses automatic age and atomically checks baseline active version; an advanced state is served without consuming another artifact. Legacy clients retain bounded wait; automatic return policy remains backend-owned.

`discovery.schema.composition_facts` scopes pure profile-derived memoization to one composition through ContextVar. Retained profile references prevent identity reuse; finally resets facts. Performance-only row hashes match. Stage timings separate radio selection, rows, portraits and quality. No process-wide profile/feed cache is added.

`NEATIE_HOME_DISCOVERY_TRIAL` defaults off. Enabled: global novelty is diagnostic (`novelty_enforced`, `novelty_would_reject`); exact-repeat and other artifact quality contracts remain. Album ranking shares first-shelf artists with the second, reduces familiar bonus and balances exposure without deleting qualified candidates. Bounded shallow-pool backfill remains. Worker reserve records profile-related playable artist counts/relation rejections; lightweight API checks reuse persisted assessment. No second engine/job owner. Real-inventory replay gives mixed variety results; discovery quality is not accepted.

## October 23 automatic Home return promotion (deployed)

CatalogScreen reuses the launch-token request on bounded Home/foreground returns. Backend `promote_ready` owns FIFO completeness/expiry admission, revision/token fencing and the 1,800-second automatic minimum inside its immediate transaction. Additive `home_feed_state.active_since` defaults to zero for legacy rows and changes on initialization/promotion independently of refill timestamps. Legacy rows can rotate once immediately; subsequent automatic rotations obey the stored clock across restarts/tabs/devices. Manual refresh bypasses age. Existing successor/job owners remain; no new timer/provider path. Open shelves/details use metadata-only checks; unchanged responses retain rows/paging/scroll. `home-return` diagnostics record outcome; feed diagnostics expose age policy/eligibility. Rollback leaves the old-reader-compatible extra column.

## October 23 aggregate metadata budgets and memory evidence (deployed)

`storage/cache_runtime.py` remains the sole generic metadata-cache owner. Its 32 MiB estimated retained budget now spans **all namespaces within each** of Search, details and embeddings, rather than multiplying with namespace count. Existing per-namespace 128-entry bounds and expiry remain; monotonic access timestamps choose family-level LRU eviction. This does not merge the three owners, add a second cache, change persisted results or evict audio/pinned streams. Cache accounting remains an estimate of admitted objects, not total process RSS or arbitrary later mutation.

`memory_runtime.py` is opt-in `NEATIE_MEMORY_DIAGNOSTICS=1`, default off and explicitly enabled in the approved AWS API/worker release. Existing cache maintenance supplies the periodic tick; a single daemon samples once a second only while a bounded inventory/extractor/solver span is active. At most 16 spans, 128 thread-child lists and 32 child RSS reads are observed per sample; no heap traversal, process command line or environment dump. Numeric cache counts/estimates use nonblocking locks. Fixed operation names and locally generated IDs plus validated 12/32-character hexadecimal resolution correlations are allowed; no account/request payload enters these records. Span cleanup survives exceptions, logging failures are best effort, and child exit races are tolerated. Normal serving/cache behavior remains independent of telemetry availability. API/worker limits are 2.25/3 GiB; live finite-limit/child observations pass, but sustained peak/allocation ownership remains open.

## October 23 cold-delivery and queue contracts (deployed)

The production-configured October 23 release enables the existing separate API origin for media; control/auth/storage retain their prior routing. Exact source/configuration and both public bundle checks pass. Test tools exercise the current expanded player rather than the retired Queue page, and distinguish the bounded diagnostic summary from its complete archive. No growing-length streaming prototype or new resolver is enabled.

Canonical playback routing now treats both `cache_version` and `stream_version` as representation pins. Errors from a pinned request retain their original status/code, especially `410 playback_source_expired`; trying another source cannot satisfy that pin. Unpinned before-response source fallback remains tested and unchanged. This is one condition in the existing route owner, not a new recovery chain. The client already discards an expired prepared descriptor before explicit Retry.

MusicApi retains one API/control client and canonical descriptor store. Optional `NEATIE_MEDIA_ORIGIN` accepts only a root HTTPS origin (HTTP only for loopback-to-loopback fixtures). `playbackUri` remaps only trusted application `/playback/stream/` paths, retaining every source/session/version/trace query; no provider URL, metadata request, saved scope or auth callback moves. Existing prepared-descriptor validation remains against the API origin. WebPlayer applies transport routing immediately before native load; synchronous Play stays within the original gesture. Empty configuration restores shared routing. A distinct hostname alone is not proof of a distinct HTTP/2 connection; benchmark CDP connection IDs verify the tested existing origin.

The existing cache-hint owner holds optional work for at most two seconds at startup, cancels only its own active abortable request, and retains interrupted bounded intent for later draining. No cancelled hint receives a failure retry delay. Per-attempt tickets prevent an older Play completion releasing a newer hold; scope changes abort/discard old work. Current preparation excludes duplicate cache-only hints through the existing owner; queue/full-cache preparation, user Search and visible image loading retain their contracts. Play completion/pause/deadline resumes hints; disposal releases without scheduling more work. No global request scheduler or retry ladder is added.

PlaybackTraceMiddleware publishes numeric expected body length and validated byte range through Server-Timing; CORS exposes existing version/range/correlation headers. BrowserAudio records expected body/range and marks completed ranges up to 2 KiB as probe-only. Timing still describes completed ResourceTiming entries, not unobserved in-flight full transfers; probes cannot be presented as whole-file delivery. Route/probe facts persist in the existing bounded summaries and complete archive. No diagnostic upload, cutoff or new storage owner is introduced.

QueueCoverBrowser constrains hit testing to the artwork stack's width/back-sleeve envelope while its parent keeps panel scrolling. Existing continuous ScrollController physics, mobile reversal, cyclic indexing and append/fraction retention remain. Same-size playback changes animate to the nearest cyclic successor; deleted front sleeves reveal a depth/scale transition rather than jumping. Deliberate swipe exit commits through revision/current-track guards; list rows collapse occupied space before committing, while overlapping cover sleeves advance immediately after exit. No audio reload or queue ownership change. All motion respects reduced motion.

Live WebM and fragmented AAC are comparison tools only. Their early output does not satisfy the current finite-duration/arbitrary-range seek contract. Existing provider-byte pinning and completely validated cached representations remain authoritative; no growing-file representation is published.

## October 22 RAM expiry and persisted retention

The existing API/worker 60-second cache maintenance now also sweeps generic Search/detail/embedding caches, recommendation track details, the expired Home candidate slot and the existing in-memory session store. Canonical Search releases snapshots after 30 minutes without use, respecting pending/in-flight work and its existing 96-entry/32 MiB bound. Durable snapshots remain reloadable through the same Search owner. Redis expiry stays with Redis. Audio bytes/files, pinned stream metadata and in-flight playback are outside this sweep; feature-cache TTL/LRU bounds described below remain independent. Releasing Python objects makes memory reusable but does not promise an immediate RSS reduction.

`runtime_retention.py` remains the single reference-aware disk owner: preserve active/ready artifacts, working-inventory and nonterminal-job references, 12 candidate/radio generations per profile, three feed artifacts, unexpired/recent/fallback windows, active/referenced models, 24 recent versions per kind and seven-day model/job windows. Acquisition recovery, account/history/library state and cached audio are not deletion targets. Bounded artifact batches cannot delete snapshot inputs belonging to artifacts left for a later batch. No schema or inventory-codec change is introduced.

Online mode requires WAL and plans/deletes within one deferred transaction; any intervening writer causes the read-to-write upgrade to fail and roll back the whole attempt. It never retries stale IDs. Daily maintenance runs in quiet hours only after one hour without recorded demand and with a fresh verified backup receipt; limits are 500 deletions per table/60 seconds, short writer wait, 50% CPU and 768 MiB process-group memory. It defers on missing demand/backup, contention, invalid references or budget failure, without stopping serving processes. Systemd timeout kills the maintenance group. The initial authorized offline run may compact freed pages; recurring deletion reuses SQLite free pages and does not perform online VACUUM. Verified daily backups keep three copies; source/image/web rollback and migration/private recovery checkpoints remain protected.

To bound repeated inventory decoding, this same helper owns `retention-reference-cache.json.gz` beside SQLite. Kind plus SHA-256 of the exact stored bytes keys each projection. Only the existing full decoder can create a projection; its references are stored as SHA-256 membership hashes with a checksum, then intersected against current retention identities. Hash collisions can retain an extra row, not remove a real dependency. Changed/corrupt/truncated entries revalidate original inventory. Bounds: 512 entries, 16 MiB compressed, 64 MiB decoded JSON; private atomic replacement under the database owner's UID. Safe verified-byte projections survive a WAL-aborted plan, but deletion IDs do not. The wrapper permits two fresh 75-second subprocess attempts for contention/budget, inside a 180-second service limit. No application thread depends on this projection, and it is not another inventory owner.

## October 22 bounded resources and application idle

`feature_store` retains one hot cache owner. Each of its four buckets is LRU ordered, capped at 512 entries/16 MiB estimated retained size by default, with per-value traversal/admission bounded to 1 MiB/8,192 nodes. Oversized data remains persisted and bypasses the hot cache. Successful reads move existing entries; writes expire unvisited stale entries and evict before admission. A 60-second expiry sweep runs in API lifecycle/worker maintenance, including idle. Bounds are retention estimates, not a promise of total process RSS or immunity to mutation of shared nested values. `AURALIS_FEATURE_HOT_CACHE_ENTRIES/BYTES` tune the existing owner; persisted inventory/admission is unchanged.

`model_runtime.refresh_collaborative_model` serializes training and rechecks the persisted signature after acquiring its lock. Background refresh reuses matching models across repeated calls/restarts. Signatures contain the original event/search aggregates plus training contract/configuration and a six-hour recency epoch (`RECOMMENDATION_MODEL_MAX_AGE_SECONDS`). Scheduler checks do not force retraining; explicit admin training still may. Normal API processes configured with an external worker serve persisted/cached models instead of starting another background trainer/bootstrap. No model/version bodies are pruned. Durable job claims retain their transactional race/attempt/lease rules; empty lane polls return from a read without requesting the SQLite writer.

`recommend.workload` owns only optional-work idleness, shared through one `background-demand.touch` operational file alongside the existing SQLite store. It stores no accounts or listening history. The API's existing trace middleware notes recognized user requests, debounces touches to at most one per five seconds off the request thread and excludes health, static artwork, trace polls and OPTIONS. Startup provides a grace period. Missing/unreadable markers fail active. `NEATIE_BACKGROUND_IDLE` defaults off and is enabled for both production consumers; `NEATIE_BACKGROUND_IDLE_SECONDS` defaults to 1,800. The worker checks demand every five seconds while idle; optional candidate/radio/artwork claims and periodic sync/train/eval/charts wait, while existing leased work completes. Interactions and composition continue; cache expiry/30-second worker heartbeat remain. `idle` is a healthy worker status. This never stops/hibernates the host, pauses audio, deletes feed state or requires client heartbeat timers. Quiet buffered/locked audio may coexist with optional-work idleness safely.

The existing queue-cache endpoint accepts `queue_cache_status_only`: it reports an exact retained plan revision or `missing`, without creating/replacing a plan, reading metadata or submitting acquisition. The web controller sends full intent only on changes/missing plans, keeps bounded status polling while foreground/playing and preserves account/revision/pause fences. Full plan/preparation ownership and source/cache identities are unchanged.

Web VinylRecord delegates only decorative rotation to `vinyl_surface_web` via a conditional import; Flutter keeps selection, controls and audio authority. An inert `HtmlElementView` paints CSS gradients/grooves and supplied artwork with bounded alternative timing. CSS owns rotation without a Flutter frame ticker; pause preserves its angle, reduced-motion CSS plus Flutter lifecycle/TickerMode disable motion, and close removes listeners/timers/surface. VM retains the prior painter/animation path. No second audio element, playback engine, recommendation path or UI polling loop is added.

## October 22 local frontend performance ownership

`CheckpointSessionStore` extends the browser store contract without changing non-browser stores. Full version-1 session JSON includes a fresh checkpoint stamp. Small per-second position records carry that stamp, selected track and index; restore accepts only exact matches, finite nonnegative position and valid duration. Malformed/foreign/stale checkpoints cannot replace a usable full session. Explicit mutations, pause, seek (including zero), failures, lifecycle and disposal retain full saves; older frontends ignore the optional stamp/position key. Rollback must retain both keys. `WebPlayer.progress` owns ordinary timeupdate/progress notifications; identity/queue changes still use the existing notifier. Sliders and lyrics subscribe to progress, while guarded pages/mini-player do not rebuild for it.

PlaybackDiagnostics caches encoded summaries only until evidence changes; under-limit saves encode once without JSON-roundtrip cloning. Ordinary BrowserAudio summary persistence coalesces at three seconds, with existing immediate critical/lifecycle saves. The separate append-only archive remains complete, indexed and flushed independently; no evidence cutoff, automatic history pruning or diagnostic upload is introduced.

ArtworkImage owns candidate ordering and bounded five-minute successful-source preferences (128 entries), pixel-size selection for exact identity-bound cached routes and reuse of Flutter's ImageCache. ArtworkWarmup has at most two workers per retained catalog, 24 preview items and 128 completion keys; it does not mount offscreen cards, run for inactive/obscured pages, start overlapping generations or continue through stale candidate lists. Preloading starts after first paint, uses already accepted URLs and preserves lazy rendering. Failed candidates can retry on subsequent data/activation. Missing provider metadata remains separate from image loading.

ArtistArtworkCache also owns `.size-v1-{160|384|768}.webp` objects in its existing prefix. Existing artwork endpoints accept only those sizes; reads reuse a validated derivative or materialize from the retained original. A shared two-slot semaphore and 16-million-source-pixel limit bound generation. Derivatives retain aspect ratio, do not upscale, preserve alpha and strip orientation metadata after correction; originals are never overwritten. Variant failure falls through existing client candidates, and missing bytes return no-store 404. The inner web gateway separates artwork from no-store API detail traffic. No live provider request is added to Home serving.

Queue cover children/URL resolution are stable within the visible six-sleeve window; per-scroll changes remain transforms/position/shading. Per-row edge filters retain the design but invalidate paint only for rows crossing the edge band. Existing vinyl constant painter and repaint isolation remain. Expanded-player state disables underlying Home hero motion/tickers without discarding page state, scroll or audio ownership.

## October 21 local balanced recovery contracts (pending deployment)

`discovery.ranking.balance_exposure` orders already-eligible scored items through two partitions. Comparable relevance prefers the underrepresented familiar/unseen partition; a materially stronger candidate wins and either partition can fill shallow supply. Queue provider bands use a two-band margin; Home scored candidates use the default one-point margin. The former six-hour exclusion, quarter-page repeat cap and 4.5-point track exposure penalty are replaced, not layered underneath the new ordering. Existing admission, feedback, explicit exclusions and diversity remain authoritative. The existing exposure repository returns last-shown times for composition; album penalties decay with age. No live metadata request is added to normal Home serving. Parent album restoration reapplies canonical exclusion before minimum cardinality; the final feed contract independently rejects cross-shelf album duplicates.

BrowserAudio forwards typed-source errors without a native code as `media-source-error`, fences the old Play promise and retains the selected resource. WebPlayer uses its existing two-attempt recovery budget, position/account/attempt guards and explicit-Pause/visibility rules. Native decode/unsupported failures remain terminal outside eligible recovery. A pending source-element failure also reloads on subsequent foreground Play. Recovery stage milliseconds accompany native events; no arbitrary shorter startup timeout or claim of reduced native seek latency was introduced.

One bounded PerformanceObserver captures completed stream ResourceTiming entries before the shared browser buffer loses them. Up to sixteen resource URLs/twelve entries each and thirty-two attempt windows are retained; exact URL/start/end attribution prevents a replayed stable URL from inheriting another attempt's request ID. Account switching clears observation ownership. Cache URLs remain unchanged. Caddy response handlers record request ID, upstream header latency and expected body length before `copy_response`; cancelled streams can bypass late handlers. The standard late duration/total may be zero/missing on cancellation, so retain early wall-clock start, log timestamp, actual size and expected length rather than interpreting zero as an instantaneous complete transfer.

DiagnosticArchive owns account-scoped `neatie-diagnostics-v2:<scope>` with an indexed run wrapper around unchanged NDJSON events. The original v1 database is left intact and read only for explicit full-history export, preserving old frontend rollback access. Default export reads only the selected run's index, including interleaved records and reloads; it does not scan/download the full account archive. Run metadata survives reloads; build changes, two-hour inactivity and explicit new activity after export establish boundaries, with same-track recovery retaining evidence. Summaries are reconstructed from the selected run and bounded independently; the complete archive has no byte cutoff. Duplicate unchanged delivery snapshots are omitted without dropping changed measurements. Failed writes remain pending/exportable. No automatic history deletion or new diagnostic upload exists.

## October 21 current recovery and search metadata ownership

BrowserAudio retains a pending seek across native `load()` and retries it on metadata/readiness. Reload notifications run after synchronous event dispatch, fenced by attempt/trace/source; WebPlayer retains confirmed position before recovery and applies explicit/remote seek intent through the same controller. New-track loads clear the pending target, and advancement is withheld while restoration is pending. Audio remains one persistent native element.

The canonical SearchService still owns one provider inventory, ranking, immutable cursors and bounded background refresh/persistence. Initial search adds no metadata provider calls. Supplied portraits and existing detail caches seed artists; background batches use the existing upstream admission/executor/deadline, publish independently and cap per-entity retries. Album relationships use the existing exact track/album detail owners and require usable persisted tracks before new card publication. Existing artwork-cache notifications revise active snapshots by exact provider ID plus normalized name; verified proxy artwork cannot be downgraded by stale metadata batches. Search observes the existing artwork pending set without adding a queue or cache. The existing bounded frontend long-poll replaces metadata revisions and keeps separately paged results; Similar Artists remains a distinct section. No semantic model or replacement search engine was introduced by this pass.

## October 9 deployed resume and Home delivery

Native remote Play reaches the persistent HTML audio element before UI reconciliation and storage. Optional Audio Session activation and canplay can recover retained intent while hidden if the browser runs their callbacks. Permission rejection, explicit Pause, source/account fences and bounded recovery prevent automatic loops. Lack of progress is not source invalidity: reload a healthy same-session resource once before one fresh-session recovery; restore the saved position after metadata. MediaSession playing still requires actual advancing audio. No browser keepalive or hidden timer workaround was added.

Home reads persisted active rows without waiting for cloud library hydration. Library's three account-scoped reads overlap and publish only after all succeed. The canonical /recommend route passes FastAPI BackgroundTasks through the existing service; active-feed successor decode/replenishment runs after delivery, coalesced while running per scope and using current reconciled state. Missing active supply and explicit preparation remain synchronous; promotions/exposures are retained. Post-response work is recoverable on subsequent requests/existing jobs, not a newly durable job system. Welcome listens for Flutter's post-paint Home/error signal rather than first frame, with 12-second unavailable escape, authentication/navigation release and reduced-motion-aware fades.

## October 9 deployed shared latency changes

The owning web backend reuses one HomeRepositorySet per process/store path, including one-time schema/recovery setup. Listening play/complete/replay stores the event and a `reconcile_interaction` job in one SQLite transaction. A dedicated lane in the existing durable Home queue applies attribution/feedback and schedules candidate work, with existing leases/backoff/restart recovery. Candidate singleton coalescing cannot cancel this job type. Event identity fences retries; feedback carries an internal event token and logical timestamp so old playback cannot undo a newer hide. Explicit non-listening feedback remains synchronous. `NEATIE_ASYNC_LISTENING_FEEDBACK=false` opts new events back into synchronous follow-up; retained jobs still require compatible readers.

Mutable candidate and radio updates decode/merge/encode before BEGIN IMMEDIATE, then compare stored payload text at commit. Candidate conflicts return false; radio conflicts recompute at most three times. Inventory format, hashes, admission, expanded recovery and album-detail atomicity are preserved. PostgreSQL get_connection uses psycopg_pool with four checked connections/16 waiters and transaction commit/rollback on context exit; an unavailable pool dependency retains direct connections. Request logging is a bounded best-effort 256-entry daemon queue, prioritizing failed/slow diagnostics and exposing queued/written/dropped/failed/pending counters; it is not the durable listening-event owner.

Browser transport still has two capture slots. Selected/upcoming work outranks speculative background work; upcoming captures can run together. Only unpublished captures without readers may be preempted. Their partial bytes are discarded before restarting the same Job, with publication blocked during that handoff. Once a URL/bytes is published the representation stays pinned; browser context closes before capacity is released. Published work can still occupy both slots. Range, complete decode/coverage and no-splicing contracts are unchanged.

MusicApi owns POST and catalog GET timing/fencing. Diagnostics redact detail IDs, distinguish full feed/background preparation/status/row work and report fixed failure/cache categories. Real player/remote incidents outrank speculative failures within the existing size limits. These changes are locally verified; CURRENT_STATUS separates them from the serving release.

October 3 clarification: history response construction recovers exact persisted catalog/entity-source metadata; bounded provider repair runs in background. Playback evidence is retained independently from display readiness. Shared Home admission requires usable title/artist even for trusted history, and bundle schema v2 invalidates older placeholder-containing bundles for recomposition. Personal mix slices are multi-card ranked reservoirs; diversity is enforced within each card, with unique visible recording sets and bounded deep overlap. Search revision timeouts acknowledge `not_modified` without replacement surfaces; only actual revisions replace client results. See [deployment and verification](CURRENT_STATUS.md).

Last reconciled: 2026-10-09. Current ownership/contracts are retained here; diagnostic deviations belong in KNOWN_ISSUES and remaining work in NEXT_STEPS.

October 8 playback latency points 4–5 (123c126/015440d): R2StreamCache owns coalesced HEAD and conditional range reads, with a 32 KiB initial read then 128 KiB. Existing stream_chunk_cache/inflight owns both upstream and R2 prefixes; object key/ETag/length/type bind cached prefixes and continuations to one representation. It is bounded to 128 entries/32 MiB; no second cache/database/artifact state was introduced. Cached playback_session entries pin metadata for 20 minutes; expiry/object loss requires a fresh resource (410), never provider bytes appended to an old one. A fresh cache miss may fall through only before bytes. Unscoped source reuse requires 90-second lease headroom. Foreground cold resolution races once; immediate primary failure starts compatibility, known blocked sources bypass futile primary work, and both-source failure does not repeat the fallback.

Browser capture may use the initial page response or matching active-player response. It must match video, actual audio codec/format and valid unambiguous index/total; unknown lengths still require completion. Existing two capture slots, reserved foreground priority, capture identity and full remux/decode/coverage admission remain. WebPlayer keeps scope-fenced preparation expiry, refreshes visible next-two work at 60-second headroom and caps loaded-resource reuse at 20 minutes or the earlier preparation lease. Foreground/native progression refreshes do not reload healthy audio. Expired paused resume calls play on a new session within the gesture and restores position. Optional browser lifecycle/session features cannot gate basic audio. Compact diagnostics retain the latest startup/next-gap metrics independently of rotated event history. Device/provider timing and background acceptance remain open.

October 5 supply integrity: background album verification rechecks retained source proofs without acquisition, preserves the original canonical denominator and counts distinct physical songs. A canonical key-level verification cache cannot authorize differing editions/credits: each occurrence is rematched before projection. Candidate semantic revisions include ordered album track/source details; clocks/cursors alone are not revisions. Radio's existing catalog/reservoir owner reprojects cached/retained tracks from current matching proofs, including legacy physical aliases, before merging/publishing; newer corrections may replace longer stale lists. Discovery acquisition uses the reserve deficit, targeting 18 unfamiliar cards for an active rotation plus two successors. The frontier uses the publication contract's artist aliases and remains intact until the planner filters exhausted requests, then applies its four-seed bound. The six-per-visible target, 36-card storage ceiling, four-request/24-verification batch limits and independent publication contracts remain.

Radio allocation reserves two validated disjoint mixes first, then budgets the six discovery slots and familiar artist slots independently. Valid familiar fallbacks precede extra discoveries, preventing one row from consuming the unfamiliar reserve intended for its successors. Three persisted-profile simulations pass; actual backend promotions remain a separate acceptance gate.

Historical October 5 storage audit, superseded by the completed codec below: immutable Home snapshots serialized full inventories, including acquisition ledger/results; the mutable feature-store inventory also retains those payloads. Some items serialize both tracks and canonical_tracks, radio payloads repeat selected cards within the reservoir, and radio diagnostics can retain megabytes. A future shrink must use the existing owners/serializers and preserve complete persisted details, original coverage denominators, relationship/source proofs, scope/version, exposure and restart recovery. Keep required active/ready/fallback/job dependencies; expiry alone is not authorization to delete. No serializer/retention change was made by the audit.

## Lossless inventory storage and ownership cleanup

The owning web backend uses storage/inventory_codec.py only at immutable Home snapshot and mutable candidate/radio feature-store boundaries. NEATIE_COMPACT_INVENTORY_WRITES is opt-in/default false and enabled on AWS after reader-first rollout. API/composer/worker consume expanded logical objects; session caches and album catalog/details stay expanded. Repository put hashes the original envelope before encoding; _from_row expands before readers. Candidate persistent read/CAS and radio load/exposure/expansion merges decode; all writers share this codec. Corrupt compact state aborts updates instead of replacing it with empty inventory.

v1 is canonical JSON compressed with zlib level 1/base64 and kind/length/SHA-256. v2 additionally removes only exact duplicate album track lists and replaces fully matching selected radio cards with ordered reservoir indices inside the compressed body. Strict equality preserves boolean/integer, float representation, edition and proof differences. Readers restore complete fields and independent mutable lists/cards, including repeated selections. Both stored and complete logical byte lengths/checksums are validated; expansion is bounded before copying. Legacy raw, literal marker collisions and v1 remain supported.

Encoding streams JSON; only 16 KiB–64 MiB bodies saving at least ten percent including envelope/base64 are wrapped. Other bodies remain literal. Snapshot IDs, scope/profile, generation/expiry clocks, logical and semantic hashes, source proofs, artwork, denominator/detail payloads, exposure and acquisition recovery remain unchanged. Maintenance changes only payload_json with a compare-and-set against its original text, validates full roundtrip/hash first, and supports expanded legacy recovery. Existing rows were converted and SQLite physically shrunk; CURRENT_STATUS records exact evidence. There is no new recommendation subsystem or artifact quality state.

Six obsolete Search-memory/radio helpers are retired; useful assertions target current catalog memory/source verification and persisted radio expansion. Earlier unused canonical resolver, intent-delta/profile/backfill roots and domain/candidate_expansion remain retired. Active source/candidate/ranker, durable events/preferences, profile hydration, inventory intent_version and acquisition cursor/results/backoff owners remain. Shared SearchPageNotifier still owns client page state; native FFI/local playback and just_audio streaming retain different roles.

Model serving owns recommendation_models/global, while version records own reporting/recovery; equal signatures do not prove equal bodies. Whole snapshot, old-model body, acquisition and backup pruning still require reference/recovery proof and are not part of this conversion. Main's older backend/local historical database is not silently migrated; use compatible backend images before opening new compact runtime archives. 396 checks pass in each writer mode, with hosted restart/compatible rollback and actual-record expanded recovery; device and full system restoration remain separate.

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
| `playback_diagnostics.dart` | Bounded, redacted, account-scoped failure snapshots and portable report formatting |
| `music_api.dart` | Shared HTTP transport, authentication and account scope |
| `web_auth.dart` | Browser Supabase session, email/Google login and recovery |
| `web_library.dart` | Account-scoped cloud likes and playlists; gated targeted playlist writes |
| `catalog_api.dart` | Canonical search, recommendation and detail contracts |
| `catalog_screen.dart` | Search and Home, authoritative revision replacement, bounded polling and separate paged inventory |
| `catalog_detail.dart` | Artist and album detail views |
| `home_widgets.dart` | Web-only Home cover/hero, circular artists, collection collages and track-strip presentation; no acquisition or readiness decisions |
| `account_screen.dart`, `library_screen.dart`, `web_app.dart` | Responsive account/library/navigation surfaces |

Browser playback reuses an already prepared source. Gesture-sensitive playback, media-button actions, foreground return and UI/queue reconciliation must agree on the same track, operation and audio element. Stored position/duration restore is local and account-scoped; it is not cloud device handoff.

WebPlayer remains the single preparation owner. Search is sequential/spare-capacity; next two queue startup buffers run independently with at most two queue preparations in flight. Same-track work coalesces within account scope, old callbacks are fenced and failures have a 30-second cooldown. Full caching is one separate background request after useful startup buffers are prepared; it cannot hold the queue pump. Explicit Play still reaches one HTML audio element within the gesture; provider capacity/foreground priority remains bounded.

October 8 exact-resource prefix reuse hashes resource URL/headers/MIME/transport; capture identity is required for browser audio, while background/priority query changes preserve it. Legacy/unbound/different-resource prefixes are ignored. Matching pinned prefixes yield before opening the same-resource continuation, with range/length/type/ETag guards and unchanged full capture validation. R2 prefix tuning and cold-provider/readiness/expiry work remain pending; bounded reports expose native startup, natural-next gap and preparation evidence.

The Oracle browser-audio adapter is an optional compatibility layer, not a replacement source system. Bounded byte requests/retries preserve source identity. Capture completion follows expected-byte validation and pending writes, rather than waiting for playback to end; media validation precedes durable cache admission. Upstream cold preparation can still be slow.

Failure diagnostics retain source/session identity and useful failure history. Compact backend summaries distinguish queue wait, preparation and transfer. These mechanisms improve evidence and recovery; they do not establish that every reported iPhone failure is fixed.

October 8 deployed playback correction (2026-10-08-playback-reports): BrowserAudio owns native progress phases; WebPlayer owns playback intent and bounded resource renewal. UI/Media Session publish playing only after non-seeking time advances with usable media and no native interruption. A foreground stalled resume may reload one fresh session and restore its position; it never splices new bytes into the old resource. Explicit Pause, denied permission, account changes and stale operations prevent automatic recovery. Hidden observation deadlines are suspended and restarted on foreground; browser termination cannot be recovered by timers. Diagnostics retain four incidents/24 events each, export at most 32 KiB and keep at most 64 KiB locally, with guarded native file share and download/summary alternatives. Optional Audio Session/Media Session/share features cannot gate basic HTML audio. CURRENT_STATUS identifies deployment and remaining physical-device limits.

Supabase remains the account/library authority. Existing likes/playlists can be loaded and played. Playlist mutation support is implemented but disabled by default (`WEB_PLAYLIST_WRITES=false`) pending Android sync rollout and concurrent-edit validation. Google/email are enabled; GitHub remains hidden unless explicitly configured. See the acceptance gates in [NEXT_STEPS.md](NEXT_STEPS.md).

The outer Caddy handles HTTPS and routes to the API/static preview gateway. The web gateway exposes the existing search/playback/catalog/recommendation contracts. Installability, offline downloads and explicit cross-device handoff remain future work.

## Home Feed Architecture

October 4 deployed H10–H14 presentation: the web shell forwards visibility/tab activity to Home. A return checks existing queue status without long polling, at most once per 30 seconds and with in-flight coalescing; populated rows/scroll stay stable until explicit refresh. Empty Home can recover the ordinary backend artifact. Backend diagnostic lanes remain the sole lane contents; the client does not infer lanes from titles or refetch providers. The album hero combines persisted qualified album rows, respects reduced motion/inactive Home and only plays supplied embedded previews within the gesture; otherwise it opens persisted detail. Home likes consume the existing scoped library. Track options and collection details use existing player/catalog callbacks; playlist writes remain gated. CURRENT_STATUS owns the serving build; device acceptance is pending.

October 5 visual layer: `app/lib/web/design_system.dart` owns the web palette, typography and themes, with locally bundled Manrope/DM Serif Display and OFL licenses. `home_widgets.dart` owns the daily pick, album spotlight and artist/card/track presentation; `editorial_placeholder.dart` keeps missing covers explicit after exact alternatives fail. `web_app.dart` opens Home and selects a sidebar or bottom navigation at 850px. `playback_dock.dart` consumes the existing WebPlayer/BrowserAudio: the desktop dock and phone modal control one audio element. Seek, repeat, queue, likes, loading and interruption messages retain their callbacks. Hover/carousel motion respects reduced-motion settings. Account, library and details retain data/write gates. This layer does not acquire provider data, admit inventory, promote feeds or change source verification.

October 5 refinement retains each scoped page inside IndexedStack. PageEntrance animates tab entry without remounting CatalogScreen or playback and disables tickers on hidden pages. NeatieLoading is a shared equalizer with a live-region label and static reduced-motion state. Images fade when their actual network frame arrives; existing exact-artwork alternatives and failure behavior are preserved. Home paging arrows invoke the existing moreHome path; Search paging buttons retain more(type). Ready successors use a dot on Refresh rather than a second promotion control. The album hero appends a duplicate first card and jumps to physical page zero only after the forward swipe settles, so logical selection/open/play remain aligned during the loop. Browser icons/manifest are branding metadata; W07 update/cache/install acceptance remains separate.

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
