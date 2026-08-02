# Next Steps

Last reconciled: 2026-08-02

## Immediate Rule

Do not start another architecture rewrite. The latest device evidence has already identified the remaining faults in the current canonical search/artwork path. Replace those faults in place, verify them, and only then continue to feed rotation or structural debt.

## P0: Preserve the Current Checkpoint

The working tree is intentionally large. Before another implementation pass, commit it in logical reviewable batches rather than one opaque checkpoint. The recommended batches are listed in the final handoff response and can be reconstructed from `git status` plus `CURRENT_STATUS.md`.

Current facts:

- Branch: `travel/work`.
- Head: `9fd0956`; the `pubspec` and launcher dependency/asset changes are already committed.
- `travel/work`, `develop`, and both corresponding origin refs currently share that head before the uncommitted work.
- Leave `resources/app_icon.png` and `resources/neatie_3rd.png` uncommitted unless they are intentionally retained as design sources.
- Commit `AGENTS.md`, `README.md`, and `docs/handoff/` only after reviewing this reconciliation.
- Push `travel/work` after each batch.

If another portable archive is made, include `.git` plus the ignored `.env`, `runtime/`, `app/android/app/src/main/jniLibs/`, and the cookies file referenced by `AURALIS_YTDLP_COOKIES_PATH`. Those paths are not restored by Git.

## P0: Final Canonical Search and Artwork Correction

This must replace flawed behavior in the existing path. Do not add a second resolver, another artwork quality state, or another Flutter cache.

### 1. Prevent canonical-evidence starvation

- Keep entity classification provider-only.
- Start the MusicBrainz recording lookup while provider work is still running, or immediately when the direct track branch supplies an exact-title candidate.
- Consume recording evidence only after the entity type is locked to `track`.
- Cancel/ignore irrelevant evidence for artist/album intent.
- Record a compact canonical outcome (`hit`, `timeout`, `empty`, or `ambiguous`) in the existing single search summary.

Acceptance:

- `In Bloom` resolves the Nirvana recording when provider and MusicBrainz evidence agree.
- A genuinely unresolved same-title query remains neutral.
- An artist query cannot be contaminated by a track branch.

### 2. Remove client-invented target identity

- Delete the `SearchPageState.fromJson` fallback that promotes `normalizedTracks.first` to Top result.
- Render Top result, lead artist, containing album, and target-derived sections only from the backend's accepted target bundle.
- A neutral backend response may still show ordinary Songs/Albums/Artists result sections, but it must not pretend that the first row is authoritative.

### 3. Bind accepted tracks to authoritative artist and release records

- Carry MusicBrainz artist, recording, release, and release-group IDs through canonical resolution.
- Match the accepted track to an already-fetched provider album first.
- Use the existing conditioned album resolver only to hydrate a track identity that is already accepted; it must not confirm the identity it was conditioned on.
- Publish a containing album only when it has a stable provider/detail identity and a usable persisted tracklist.
- Coalesce the credited artist through authoritative MusicBrainz/provider alias evidence.

### 4. Replace optimistic entity artwork admission

- Reuse/generalize the existing verified artist artwork cache for albums and playlists.
- Do not treat `/entity_artwork/{token}` as proof until bytes are verified in R2/local cache.
- Persist verification state and bounded failure/retry state by canonical entity identity.
- Try alternate source URLs in bounded background work.
- On success, update the canonical catalog record and notify active search snapshots so the card appears or improves at a new revision.
- Make album detail hydration call the same persistence path; opening a detail page must not be the only screen that learns the artwork.
- Avoid per-card blocking `HEAD` fan-out in the normal response path.

### 5. Make progressive snapshots semantic and monotonic

- Fingerprint user-visible target and surface identities plus artwork tokens.
- Increment/store a revision only when that fingerprint changes.
- Merge new verified cards without removing already visible valid cards.
- Stop polling when surfaces are complete/exhausted or the bounded retry schedule ends.
- Flutter should log progressive enrichment only when the visible fingerprint changes.

### 6. Finish log cleanup

- Replace the current single-tag emulator suppression with a working `ANDROID_LOG_TAGS` filter around `flutter run`, while preserving Flutter application logs and warnings/errors.
- Collapse Flutter recommendation request-body, request-start, timing, and full diagnostics success logs into one compact success line.
- Keep request failures, timeouts, target-contract failures, and aggregated source-registry failure summaries.
- Remove the repetitive R2 stream-cache enabled success line; keep disabled/error state.

### 7. Verification before the next device audit

Run the focused backend tests after Python is installed/activated. Add/adjust tests for:

- early-but-type-gated canonical evidence;
- no Flutter/backend target fabrication contract;
- containing album identity propagation;
- unverified entity artwork not being publishable;
- entity artwork persistence and snapshot notification;
- semantic no-op revisions;
- monotonic progressive merges.

The user then runs `flutter pub get` and `flutter analyze` locally.

## P0: Device Audit After the Correction

### Search fixtures

Run each query once, repeat it, then restart the backend and run it again:

- `The Trooper`
- `Eric Clapton`
- `Dio`
- `Nirvana`
- `Come As You Are`
- `In Bloom`
- `Don't Stop Me Now`
- `Michael Jackson`
- `artic monkey`

For every query record:

- time from submit to first complete result;
- backend `ytmusic` time;
- backend canonical/catalog time;
- total request time;
- chosen canonical lead;
- track/album/artist-work/related-artist/playlist counts;
- whether artwork is present immediately;
- whether repeated/restarted search reuses persisted artwork/catalog state.

Acceptance:

- `The Trooper` leads to Iron Maiden.
- `Dio` leads to the established heavy-metal artist, not a low-authority same-name artist.
- `Nirvana` leads to the established band, not another same-name artist.
- `Come As You Are` follows Nirvana identity.
- `In Bloom` resolves the Nirvana recording and never builds artist surfaces for a lower-authority same-title credit. If authoritative evidence is unavailable, it remains neutral rather than guessing.
- `In Bloom` shows Nirvana as the credited artist and Nevermind as the containing album from the backend target bundle, not a Flutter first-track fallback.
- `Don't Stop Me Now` resolves Queen; completed artist albums/tracks do not disappear from a later snapshot.
- `Michael Jackson` keeps its correct artist catalog, produces related artists when verified, and does not require opening album detail pages to populate artwork.
- `artic monkey` resolves Arctic Monkeys.
- Artist works follow the selected artist.
- Related artists are genuinely related and canonical-deduplicated.
- Albums and artist works have useful artwork.
- Every visible album/playlist artwork URL returns image bytes; a proxy path alone is not success.
- Every displayed album opens to a playable/resolvable tracklist.
- No later page degrades into unrelated provider noise.
- Regular Albums for an artist query do not masquerade tribute/sleep/cover compilations as the artist's own discography.
- Snapshot revision does not advance when visible content is unchanged.
- Known/local searches are materially faster; slow unseen upstream searches should still aim below ten seconds without losing correctness.

### Feed fixtures

- Cold launch with copied runtime state must show the last valid full feed immediately.
- Established account should show all ten applicable rows.
- Refresh must keep the old feed visible until a changed valid feed is promoted.
- Popular Radio should retain correct canonical artist artwork.
- Quiet Picks should page cleanly to the client maximum without duplicate visible recordings.
- Release years should remain stable across refresh.

### Playback fixtures

- Play the first track from Recently Played.
- Let the second and third tracks advance automatically.
- Manually skip forward twice.
- Start a second queue from Search.
- Compare an R2 hit with a never-before-resolved source.

Acceptance:

- no silent second/third queue item;
- prepared/R2 sources should approach sub-one-second start;
- a failed YouTube source is not retried indefinitely;
- queue metadata and artwork remain attached to the canonical track.

## Work Order After the Audit

### 1. Complete the corrected search/artwork device gate

If the audit still fails, use the exact target evidence, canonical outcome, artwork state, and semantic snapshot fingerprint. Do not add another search route or client-side target fallback.

### 2. Replace single prepared-feed behavior with a ready queue

Agreed product design:

- remove forced feed refresh simply because the user returns from Search;
- maintain at least two ready future feeds;
- build backups from a balanced blend of retained canonical inventory and newly acquired candidates;
- do not create backups by merely shuffling the same visible tracks;
- pull refresh promotes the next ready feed immediately;
- promotion schedules a replacement so the ready queue replenishes;
- opening the app can consume a newer ready feed according to a clear age/rotation rule.

This is a backend FeedState replacement. Flutter should only request/present the next server-approved version.

### 3. Finish playback continuity and latency

- diagnose the silent second-track queue transition with tap/resolve/buffer/audio-session timings;
- persist bounded starting prefixes across backend restarts;
- prioritize user playback over background verification;
- serve local prefix/R2 before opening a live upstream request;
- retain alternate verified sources;
- keep background warming bounded so it does not trigger YouTube bot checks.

### 4. Popular Radio correctness and rotation

- canonical seed artwork for every card;
- preserve a complete old card while replacement artwork/catalog is incomplete;
- independent radio fingerprint/rotation;
- twelve-track publish minimum, enrich toward twenty-four;
- visible cross-card and main-feed duplication checks.

### 5. Complete artist profiles

- MusicBrainz-defined public discography;
- playable-resolution status per recording;
- paged songs and releases;
- canonical biography and artwork;
- legitimate provider statistics only;
- related artists and complete album metadata.

### 6. Metadata completion

- continue persisted release-year/release-date enrichment;
- avoid repeated lookups for exhausted entities;
- never substitute YouTube upload year for release year without evidence.

### 7. Behind the Lyrics

- verify lyrics payload transfer;
- distinguish model/provider failure from low-context interpretation;
- replace generic fallback with actionable failure state;
- improve structured interpretation and canonical caching.

### 8. Replace Search landing placeholders

- live charts and trends;
- country/platform/time filters;
- animated line/area graphs;
- movement and rank statistics;
- canonical playable drill-down.

### 9. Reduce structural debt after the behavior gates

Do this after the device audit and the higher-priority feed/playback correctness work. Keep each change behavior-preserving and covered by focused tests:

- decompose the transitional `ingestion/python_proxy/server.py` service locator into explicit composition/configuration boundaries without introducing parallel search, recommendation, or playback paths;
- split oversized backend and Flutter core files by existing domain responsibility, starting with the highest-change/highest-coupling modules rather than performing a broad rewrite;
- remove the dormant Flutter keyword-based home-lane fallback so backend-provided lanes remain the only customer-facing lane composition path.

## Focused Verification Commands

Backend search:

```powershell
python -B -m pytest ingestion/python_proxy/test_search_enrichment.py ingestion/python_proxy/test_search_latency.py -q
```

Recommendation/feed:

```powershell
python -B -m pytest ingestion/python_proxy/test_candidate_inventory_materialization.py ingestion/python_proxy/test_discovery_engine.py ingestion/python_proxy/test_structured_recommendation_refactor.py -q
```

Source and playback:

```powershell
python -B -m pytest ingestion/python_proxy/test_source_registry_pipeline.py ingestion/python_proxy/test_stream_prepare_latency.py -q
```

Contracts/catalog:

```powershell
python -B -m pytest ingestion/python_proxy/test_api_contracts.py ingestion/python_proxy/test_catalog_normalization.py ingestion/python_proxy/test_catalog_acceptance.py -q
```

Flutter:

```powershell
cd app
flutter pub get
flutter analyze
```

Do not run every backend test for a one-file change by default. Run the focused concern first, then the combined regression gate before a device build.

# Fresh Windows Development Machine

## Required software

Install these before expecting `start_dev.ps1` to work:

1. **Git and GitHub Desktop**
   - Git provides the repository and branch history.
   - GitHub Desktop provides the travel commit/push workflow.

2. **Codex desktop app**
   - Sign into the same ChatGPT account.
   - Open the extracted repository as a local project.
   - The old Codex conversation will not be inside the ZIP; use this handoff.

3. **Flutter stable**
   - Install the stable Windows Flutter SDK.
   - Add `<flutter-sdk>\bin` to `PATH`.
   - The app requires Dart `>=3.2.0 <4.0.0`.

4. **Android Studio and Android SDK**
   - Android SDK Platform required by the installed Flutter stable version.
   - Android SDK Build-Tools.
   - Android SDK Platform-Tools (`adb`).
   - Android Emulator.
   - Android SDK Command-line Tools.
   - NDK (Side by side) and CMake only if rebuilding `libauralis.so`.
   - Android Studio’s bundled JDK 17 is compatible with this project.

5. **Android emulator**
   - Create an x86_64 Medium Phone-class AVD using a current Google APIs image (API 35/36 is suitable).
   - Start it before running the repo script.
   - The script currently targets `emulator-5554`, normally the first running emulator.

6. **Python 3.11 x64**
   - Python 3.11 matches `ingestion/python_proxy/Dockerfile`.
   - Enable the installer option to add Python to `PATH`.

7. **FFmpeg**
   - Install `ffmpeg` and `ffprobe`.
   - Add their `bin` directory to `PATH`.
   - yt-dlp playback preparation and song recognition require them.

8. **Ollama (optional for core feed/search, required for local assistant work)**
   - Install it only if testing Ask Neatie or Behind the Lyrics.
   - The configured model names and endpoint come from `.env`.

9. **Docker Desktop (optional)**
   - The backend can run from Python directly.
   - Docker is useful only if the user wants the `python:3.11-slim`/FFmpeg container path.

## First verification

Open a new PowerShell and run:

```powershell
git --version
python --version
ffmpeg -version
flutter --version
flutter doctor -v
adb --version
```

Then:

```powershell
flutter doctor --android-licenses
```

Resolve every Android toolchain error reported by `flutter doctor` before diagnosing project code.

## Extracted ZIP checks

After extracting the encrypted archive, confirm:

```text
<repo>\.git\
<repo>\.env
<repo>\runtime\recommendation_store.sqlite
<repo>\runtime\assistant_memory.sqlite
<repo>\app\android\app\src\main\jniLibs\arm64-v8a\libauralis.so
<repo>\app\android\app\src\main\jniLibs\x86_64\libauralis.so
```

If `.git` exists, use GitHub Desktop:

1. **File**
2. **Add Local Repository**
3. Select the extracted repository root.

Do not initialize a new repository over it.

## Python environment

An environment copied from another Windows installation may contain absolute paths. Try it, but recreate it if imports or the interpreter path fail.

As of the 2026-08-02 Codex audit, neither `python` nor an installed interpreter through the Windows `py` launcher is visible in the Codex shell. The app was previously run from a user-configured environment, but the focused test command cannot be considered reproducible until Python is installed/activated in a fresh terminal and `python --version` succeeds.

Reliable rebuild:

```powershell
cd ingestion\python_proxy
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pytest
cd ..\..
```

Run `start_dev.ps1` from the same activated PowerShell so spawned Python processes inherit the environment.

Optional backends require extra packages only when used:

- Redis session store: install `redis`.
- Google Cloud object store: install `google-cloud-storage`.

The normal local SQLite/R2 path does not require those optional packages.

## Flutter environment

```powershell
cd app
flutter pub get
flutter analyze
cd ..
```

If launcher resources need regeneration:

```powershell
cd app
dart run flutter_launcher_icons
cd ..
```

The copied `jniLibs` files provide native parity. If they are absent, the project has C++ source under `core_native/`, but there is no fully documented one-command Android native rebuild in the current handoff. Preserve the copied `.so` files.

## Private configuration

`start_dev.ps1` reads the root `.env`. Never commit its values.

### Needed for the current authenticated app

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_REDIRECT_SCHEME`
- `SUPABASE_REDIRECT_HOST`

### Needed for current structured search/recommendation quality

- `YOUTUBE_DATA_API_KEY`
- `LASTFM_API_KEY`

ListenBrainz public catalog operations do not require a token. A ListenBrainz username is stored through recommendation preferences when used.

### Needed for R2 stream and artist-artwork persistence

- `AURALIS_STREAM_CACHE_BACKEND`
- `AURALIS_STREAM_CACHE_BUCKET`
- `AURALIS_STREAM_CACHE_PREFIX`
- `AURALIS_R2_ACCOUNT_ID`
- `AURALIS_R2_ACCESS_KEY_ID`
- `AURALIS_R2_SECRET_ACCESS_KEY`
- `AURALIS_R2_ENDPOINT_URL`
- optional `AURALIS_ARTIST_ARTWORK_CACHE_PREFIX`
- optional `AURALIS_ENTITY_ARTWORK_CACHE_PREFIX` (current proxy transport; do not treat it as verified publication until the P0 fix)

### Needed for synchronized recommendation state

- `RECOMMENDATION_SYNC_DATABASE_DSN`
- `RECOMMENDATION_ENABLE_SCHEDULER`
- optional interval/evaluation variables already present in the copied `.env`

### Assistant configuration

- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- `OLLAMA_EMBED_MODEL`
- optional `OLLAMA_FAST_MODEL`
- optional `OLLAMA_THINKING_MODEL`
- optional `OLLAMA_PLANNER_MODEL`
- optional `OLLAMA_API_KEY`
- `ASSISTANT_EMBED_BACKEND`
- `ASSISTANT_VECTOR_BACKEND`
- `USE_LANGGRAPH_ASSISTANT`

### Optional playback/recognition configuration

- `AURALIS_YTDLP_COOKIES_PATH`
- `AURALIS_YTDLP_PO_TOKEN`
- `AURALIS_ACRCLOUD_HOST`
- `AURALIS_ACRCLOUD_ACCESS_KEY`
- `AURALIS_ACRCLOUD_ACCESS_SECRET`

If a cookies file is copied, update `AURALIS_YTDLP_COOKIES_PATH` to its new absolute location.

## Starting the complete development environment

1. Start the Android emulator.
2. Confirm:

```powershell
adb devices
```

3. From an activated Python environment at the repository root:

```powershell
.\start_dev.ps1
```

Expected windows:

- Recommendation Worker, when `RECOMMENDATION_ENABLE_SCHEDULER=1`
- Python/FastAPI engine on port 8000
- Flutter process in the original PowerShell

Expected URLs:

- Windows backend: `http://127.0.0.1:8000`
- Android emulator to Windows backend: `http://10.0.2.2:8000`

For a physical Android device, `10.0.2.2` is wrong. Configure `AURALIS_PROXY_URL`/`AURALIS_DEVICE_PROXY_URL` to the Windows machine’s reachable LAN address or use an explicit adb reverse setup.

## Travel Git workflow

On the travel device:

1. Extract the repository with `.git`.
2. Add it to GitHub Desktop.
3. Fetch origin.
4. Create a branch such as `travel/project-work`.
5. Commit logical, testable batches.
6. Push the branch after every useful pass.

At home:

1. Fetch origin in GitHub Desktop.
2. Check out `travel/project-work`.
3. Review/test it.
4. Merge it into `develop`.

There is no need to carry the ZIP home after the travel branch has been pushed.

## New Codex Task Opening Prompt

```text
We are continuing Neatie on branch travel/work. Read AGENTS.md and every file in docs/handoff/ completely before acting. Inspect git status and the current branch without reverting anything. The committed head is 9fd0956; the large search/artwork/source working tree is intentional. Several device passes are documented in CURRENT_STATUS.md. The current code improved fail-closed identity handling, artist artwork, related artists for Dio, progressive polling, and yt-dlp containment, but In Bloom still fails canonical resolution because recording evidence starts too late; Flutter fabricates a first-track Top result; album/playlist artwork is admitted before verification; and snapshot revisions advance without visible changes. First reconcile those claims with the current implementation. Do not start feed-queue or structural-debt work until the P0 correction and device audit pass. Do not run Flutter commands in the agent environment.
```
