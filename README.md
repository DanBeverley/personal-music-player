# Neatie

**[Try the live demo](https://neatie.duckdns.org)** · [Architecture](docs/handoff/ARCHITECTURE.md) · [Roadmap](docs/handoff/NEXT_STEPS.md)

## Overview

Neatie brings music discovery, playback and a personal library to Android and the web. Start with a song, artist or album, explore related music, shape your queue and keep favourites. Recommendations, lyrics, charts and an Android music assistant connect discovery with everyday listening.

Android combines streaming with downloads and local playback. A separate Flutter web client serves desktop/mobile browsers through the shared backend. Browse as a guest, or use Google/email accounts for cloud likes, playlists and listening records.

**Status:** active development. The demo is a working preview. Playback/search latency, iPhone background and resume reliability, and cross-device playlist consistency remain under validation.

## What Neatie offers

- **Find music:** suggestions, search history and playable history cards; Top, Songs, Artists and Albums results with detail pages. Web results update as you type.
- **Discover your next listen:** Today's Pick, featured albums, recommended artists, personal mixes, Popular Radio, recently played and frequently listened shelves. Listening and library activity inform recommendations.
- **Shape your queue:** play from search, albums, playlists or radio; add, remove, reorder and play next. Related songs extend sessions; upcoming audio is prepared and cached in the background.
- **Keep your music:** liked songs, playlists and listening history, with separate guest/account storage and cloud-backed account data.
- **Explore:** provider-backed charts and new releases, with country, period and source filters where available. Web supports approximate country detection and saved region choices.
- **Follow the words:** synchronized lyrics with line highlighting, or plain lyrics when timestamps are unavailable. Web prefetches lyrics and reuses cached results.

## Platform support

Both clients use the shared discovery and playback services. Interface capabilities differ:

| Capability | Android | Web |
| --- | --- | --- |
| Search interaction | Live suggestions; submit or select to load results | Live suggestions and results while typing |
| Playlist editing | Create, rename, delete, add/remove songs and customize covers | Existing playlists open/play; editing is gated |
| Queue song search | Search the catalogue and enqueue songs | Top song results with add-to-queue controls |
| Downloads and local playback | Download manager and device-local audio | Not implemented |
| Player controls | Seek/repeat, shuffle, sleep timer and ten-second skips | Seek/repeat; persistent desktop dock/mobile player |
| Library extras | Follow artists; save albums, mixes and radios | Cloud liked songs and existing playlists |
| Recommendation controls | Remove from feed; Neatie, blended or ListenBrainz-first taste modes | Listening/like signals; no taste-settings UI |
| Ask Neatie | Music chat, playable recommendations and playlist drafts; saved/pinned/archived chats | Not implemented |
| Song recognition | Record a snippet or import/share audio/video to find a song | Not implemented |
| Behind the Lyrics | Song interpretations and themes | Not implemented |
| Updates and diagnostics | Signed APK self-update and playback logging | Browser delivery; share/download compact playback reports |

Assistant/interpretation need a configured model provider; recognition needs ACRCloud. Downloaded audio and locally chosen covers remain device-local. `WEB_PLAYLIST_WRITES` stays disabled pending the SQL migration, Android sync compatibility and concurrent-edit acceptance.

## Architecture

- **Clients:** Flutter/Dart; Android uses Riverpod, `just_audio` and `audio_service`. Web uses HTML audio and Media Session controls, with separate UI/playback implementations.
- **API:** FastAPI/Python owns search, details, playback, lyrics and discovery. Providers include MusicBrainz, YouTube Music, Last.fm, ListenBrainz and LRCLIB.
- **Discovery:** durable background jobs enrich persisted inventory and compose Home feeds; normal responses serve prepared data.
- **Storage:** SQLite retains catalogue metadata, verified source links, search and recommendation state. Supabase owns authentication and cloud library/history data; local storage or Cloudflare R2 caches audio separately.
- **Audio delivery:** yt-dlp source resolution, bounded preparation and verified byte-range delivery. Production uses Docker/Caddy on AWS.
- **Optional services:** LangGraph/Ollama assistant integration and ACRCloud recognition.

The original C++17/miniaudio engine retains Dart FFI/CMake integration; pitch/rate effects and chorus detection remain prototypes. Internal `Auralis`/`EBB` names reflect the project's earlier branding.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `app/lib/logic/`, `app/lib/main_*.dart` | Android playback, account state and application screens |
| `app/lib/web/` | Browser interface, player, library and diagnostics |
| `core_native/` | Native audio library and FFI-facing C API |
| `ingestion/python_proxy/` | FastAPI entry point, shared backend services and worker |
| `ingestion/browser_audio/` | Legacy browser transport; authenticated playback fallback is disabled |
| `supabase/` | Account schema and playlist migration SQL |
| `deploy/production/`, `tools/` | Deployment, backup, release and verification utilities |
| `docs/` | Architecture, operations, release guidance and tracked work |

## Run locally

Install Flutter with a compatible Dart SDK (`>=3.4.0 <4.0.0`), Python 3.11, Node.js 22 and FFmpeg. Android builds also need the Android SDK/NDK, JDK 17 and CMake 3.22.1.

From the repository root, create a backend environment:

```sh
cd ingestion/python_proxy
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` in a POSIX shell, then start the API:

```sh
python -m pip install -r requirements.txt
python -m uvicorn api_app:app --host 127.0.0.1 --port 8000
```

In a second terminal, from the repository root:

```sh
cd app
flutter pub get
flutter run -d chrome --dart-define=AURALIS_PROXY_URL=http://127.0.0.1:8000 --dart-define=NEATIE_SAME_ORIGIN_API=false
```

Guest browsing needs no Supabase configuration. For sign-in, set `SUPABASE_URL`/`SUPABASE_ANON_KEY` build defines, schema and callbacks: [authentication setup](supabase/README.md). See [operations](docs/handoff/OPERATIONS.md) for provider configuration. Keep credentials, cookies, runtime databases and downloads outside Git.

The API starts an embedded recommendation scheduler by default. For a separate worker, run `python worker_main.py` from `ingestion/python_proxy/`, sharing `AURALIS_RUNTIME_DIR` with the API. Set `RECOMMENDATION_EXTERNAL_WORKER=1` in both processes and `RECOMMENDATION_ENABLE_SCHEDULER=0` in the API.

Android on Windows: configure an emulator and run [`start_dev.ps1`](start_dev.ps1), which loads `.env` and starts backend/worker. See [Android releases](docs/ANDROID_RELEASE.md) for signed builds.

## Verification and contribution

Run focused checks for the component you change:

| Working directory | Command |
| --- | --- |
| `ingestion/python_proxy/` | `python -m pytest -q test_api_contracts.py test_lyrics_service.py` (install `pytest` and `httpx` first) |
| `app/` | `flutter test` |
| `app/` | `flutter build web --release` |

Use focused feature-branch commits and integrate through `develop`; `main` promotion and deployment require separate review. The legacy deployment workflow requires explicit opt-in.

Web diagnostics record startup, resume, queue-transition and API timings, failures and cache evidence. Performance claims require device measurements; fixtures/server timings alone cannot establish audible playback speed or reliability.

See [known issues](docs/handoff/KNOWN_ISSUES.md) and [next steps](docs/handoff/NEXT_STEPS.md) for dated evidence and the task queue; [operations](docs/handoff/OPERATIONS.md) covers hosting, backups and rollback.
