# Neatie

**[Open the live web app](https://neatie.duckdns.org)** · [Architecture](docs/handoff/ARCHITECTURE.md) · [Release status](docs/handoff/CURRENT_STATUS.md)

Neatie is a music discovery and listening app for **Android** and **desktop and mobile browsers**. Find a recording, explore an artist’s work, build a queue and keep a personal library. Both clients share the same catalogue, recommendation and playback backend. Android also supports downloads and device-local audio.

The web preview currently serves build `2026-11-06-artist-home`. Guests can search and listen. Sign in to use personalized Home, cloud likes and playlists. Background playback and cross-device playlist edits are still being validated on real devices. [Known issues](docs/handoff/KNOWN_ISSUES.md) · [Roadmap](docs/handoff/NEXT_STEPS.md)

## Features

| Area | Capabilities |
| --- | --- |
| **Search and catalogue** | Search for songs, artists and albums. Browse artist releases and appearances, related artists and playable album track lists. Release dates and provider play counts appear where verified metadata is available. |
| **Discovery** | Personalized mixes, featured albums, related artists, artist and track radio, listening history and frequently played songs. Browse charts by supported regions and sources, and explore new releases. |
| **Playback** | Seek, repeat, play next and reorder the queue. Start an artist’s catalogue from a selected song, or extend a session with related tracks. Upcoming tracks can be prepared in the background. |
| **Library** | Like songs and create, rename, reorder and edit cloud playlists on supported builds. Android also saves catalogue collections. The desktop web sidebar provides quick access to playlists and followed artists. |
| **Lyrics and AI** | Follow synchronized lyrics when timings are available, or read plain lyrics. Ask about a song in the web vinyl player, browse playable suggestions or draft and edit playlists in the Library assistant. Android has a music assistant with saved conversations. AI features require a configured model and account access where enabled. |

**Web** has a vinyl player with cover and list queue views, queue search and shareable playback diagnostics. Search suggestions appear while typing. Submit the query to load the full results.

**Android** adds downloads, device-local playback, a sleep timer and song recognition from a recording or imported media when ACRCloud is configured. Signed APK releases and in-app update support are documented in the [Android release guide](docs/ANDROID_RELEASE.md).

## How it works

- **Clients** live in [`app/`](app/). Flutter shares models and contracts across clients. Android uses Riverpod, `just_audio` and `audio_service`. Web uses HTML audio and Media Session controls.
- **Catalogue and recommendations** live in [`ingestion/python_proxy/`](ingestion/python_proxy/). FastAPI serves provider-backed search, metadata, lyrics and playback. A recommendation worker prepares Home feeds from persisted candidate inventories.
- **Media and account data** have separate storage. SQLite holds catalogue and recommendation state. Supabase handles authentication and cloud library data. Local storage or Cloudflare R2 caches audio. The playback service resolves sources with yt-dlp and supports byte-range delivery.
- **Native audio and hosting** live in [`core_native/`](core_native/) and [`deploy/production/`](deploy/production/). The C++ audio library uses Dart FFI and CMake. Production runs Docker services behind Caddy on AWS. [Operations and rollback](docs/handoff/OPERATIONS.md)

## Run locally

Install Flutter, Python 3.11, FFmpeg and Node.js 22 for YouTube source resolution. Android builds also need the Android SDK and NDK, JDK 17 and CMake 3.22.1.

From the repository root, prepare the API environment.

```sh
cd ingestion/python_proxy
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` in a POSIX shell. Then start the API from the same directory.

```sh
python -m pip install -r requirements.txt
python -m uvicorn api_app:app --host 127.0.0.1 --port 8000
```

In a second terminal, start the web client from the repository root.

```sh
cd app
flutter pub get
flutter run -d chrome --dart-define=AURALIS_PROXY_URL=http://127.0.0.1:8000 --dart-define=NEATIE_SAME_ORIGIN_API=false
```

Guest web browsing works without Supabase. For sign-in and cloud playlists, follow the [Supabase setup](supabase/README.md) and use public client build defines. The live web build enables playlist editing through `WEB_PLAYLIST_WRITES` and the installed playlist SQL. Keep private environment files, cookies, databases and downloaded media outside Git.

For Android on Windows, use [`start_dev.ps1`](start_dev.ps1) with an emulator or configured device. See [operations](docs/handoff/OPERATIONS.md) for the standalone worker, providers and AWS configuration.

## Checks and release process

- **Backend** — From `ingestion/python_proxy/`, install `pytest` and `httpx` in the active environment, then run focused tests such as `python -m pytest -q test_search_latency.py`.
- **Flutter** — From `app/`, run `flutter test` and `flutter build web --release` for the affected client. Validate audible playback and background controls on target devices.
- **Release** — Integrate feature branches through `develop` and review before promoting to `main`. Follow the [deployment guide](docs/PRODUCTION_DEPLOYMENT.md) and [live runbook](docs/handoff/OPERATIONS.md) for an AWS rollout.
