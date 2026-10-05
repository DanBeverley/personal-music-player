# Neatie Personal Music Player

Neatie has an Android Flutter client and a separate Flutter web client for PC/iPhone browsers, sharing FastAPI services for canonical Search, durable Home, source-aware playback, discovery, library, Charts/Releases, lyrics and assistant/recognition.

## Start here

The six [handoff guides](docs/handoff/PROJECT_OVERVIEW.md) own current information. Read [status](docs/handoff/CURRENT_STATUS.md), [full queue](docs/handoff/NEXT_STEPS.md), [architecture](docs/handoff/ARCHITECTURE.md), [known issues](docs/handoff/KNOWN_ISSUES.md) and project overview before implementation; use [operations](docs/handoff/OPERATIONS.md) for hosting, backup or deployment.

Current serving build is `2026-10-03-history-search` on AWS. Main source tree uses develop; deployed web/backend work is in the separate codex/web-app tree. Both intentionally differ despite committed e720709. Inspect Git status and preserve unrelated changes. Complete queue: 66 entries with implementation, acceptance, recheck and deferred statuses.

## Repository

- app/: Android Flutter app and, in the web worktree, separate browser modules.
- ingestion/python_proxy/: FastAPI, recommendation worker, source/stream/Search/Home and feature services/tests.
- ingestion/browser_audio/: optional browser-backed transport in the web worktree.
- core_native/: native audio/FFI source; Android builds it through CMake/managedJniLibs.
- supabase/: auth and cloud-user schema/setup guidance.
- deploy/production/ and tools/: deployment, backup and local verification helpers.
- runtime/, .env and cookies: ignored private/local state, not supplied by Git.

## Architecture rules

Backend alone promotes active/ready Home; composition uses persisted inventory and bounded background acquisition. Popular Radio owns independent supply/contracts. Use one canonical Search/source pipeline; canonical identity is not proof of playability, meaningful artwork or usable details. Fix existing ownership/state paths instead of adding overlapping systems.

## Local work and releases

Windows Android workflow: run `.\start_dev.ps1` with configured emulator (normally emulator-5554), Python/Flutter and protected root .env. Emulator backend is http://10.0.2.2:8000; localhost inside Android is the emulator, not Windows.

Use focused verification commands in NEXT_STEPS; user runs flutter analyze. See [Android release](docs/ANDROID_RELEASE.md), [deployment guide](docs/PRODUCTION_DEPLOYMENT.md) and [auth setup](supabase/README.md). Documentation-only changes do not authorize application deployment, publishing or cloud resource changes.
