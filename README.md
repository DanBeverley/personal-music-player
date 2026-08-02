# Neatie Personal Music Player

Neatie is an Android-first Flutter music application backed by a local Python/FastAPI service. It combines personalized recommendations, canonical music search, source-aware playback, playlists, history, downloads, lyrics, recognition, and assistant features.

## Repository

- `app/`: Flutter/Riverpod Android client.
- `ingestion/python_proxy/`: FastAPI service, recommendation worker, canonical search, catalog, source resolution, playback, assistant, recognition, and backend tests.
- `core_native/`: native audio/FFI source.
- `supabase/`: authentication and synchronized user-data material.
- `runtime/`: ignored local recommendation and assistant state.
- `docs/handoff/`: authoritative current status, architecture, known issues, and next actions.

## Current Architecture

- The backend is the sole authority for active/prepared home-feed promotion.
- Home recommendations consume persisted canonical candidate inventory rather than performing broad discovery in `/recommend`.
- Search uses one canonical `/search` path and keeps canonical identity separate from playable provider identity.
- Playback resolves canonical tracks through persisted source mappings, prepared prefixes, R2, and bounded yt-dlp resolution.
- Popular Radio owns an independent artist catalog and allocation contract.
- Replaced search and recommendation paths are removed instead of retained as parallel fallbacks.

The home feed currently supports `todays_pick`, `featured_new_albums`, `last_played`, `frequently_listened`, `made_for_you`, `because_you_played`, `popular_radio`, `recommended_albums`, `recommended_artists`, and `quiet_picks`.

## Current Development State

The active work is on `travel/work`. Recommendation/feed completeness improved materially and the yt-dlp background error storm is now bounded. Canonical search correctness and artwork persistence remain behind a device-validation gate: same-title track resolution can fail closed because canonical evidence starts too late, Flutter can still invent a top result from the first track, and album/playlist artwork can be admitted before its source is verified.

Read these files before changing code:

1. `AGENTS.md`
2. `docs/handoff/CURRENT_STATUS.md`
3. `docs/handoff/NEXT_STEPS.md`
4. `docs/handoff/ARCHITECTURE.md`
5. `docs/handoff/KNOWN_ISSUES.md`
6. `docs/handoff/PROJECT_OVERVIEW.md`

## Local Development

The normal Windows workflow is:

```powershell
.\start_dev.ps1
```

The script expects a running Android emulator at `emulator-5554`, a usable Python environment, Flutter on `PATH`, and the ignored root `.env`. For Android emulators, the app reaches the host backend through `http://10.0.2.2:8000`.

Private and machine-specific state is intentionally not committed. Preserve `.env`, `runtime/`, yt-dlp cookies, and `app/android/app/src/main/jniLibs/` separately when moving machines.

Focused backend commands and the complete Windows setup checklist are in `docs/handoff/NEXT_STEPS.md`. Flutter commands, especially `flutter analyze`, must be run by the user locally because they are unreliable in the agent environment.
