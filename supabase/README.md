# EBB Phase 1 Auth Setup

## 1. Create the Schema

Run `phase1_schema.sql` in the Supabase SQL editor.

## 2. Enable Auth Providers

Enable whichever providers you want in Supabase Auth:

- Google OAuth
- GitHub OAuth
- Magic Link / Email OTP

Use these mobile redirect values:

- Redirect scheme: `ebb`
- Redirect host: `login-callback`
- Mobile redirect URL: `ebb://login-callback`

## 3. Pass Environment Values Into Flutter

You can either set environment variables manually in PowerShell or create a repo-root `.env` file.
`start_dev.ps1` will load `.env` automatically for local development.

Set these `dart-define` values when running or building the app:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_REDIRECT_SCHEME=ebb`
- `SUPABASE_REDIRECT_HOST=login-callback`

Example:

```powershell
flutter run `
  --dart-define=AURALIS_PROXY_URL=http://10.0.2.2:8000 `
  --dart-define=SUPABASE_URL=https://YOUR_PROJECT.supabase.co `
  --dart-define=SUPABASE_ANON_KEY=YOUR_SUPABASE_ANON_KEY `
  --dart-define=SUPABASE_REDIRECT_SCHEME=ebb `
  --dart-define=SUPABASE_REDIRECT_HOST=login-callback
```

## 4. Production Build

```powershell
flutter build apk --release --split-per-abi `
  --dart-define=AURALIS_PROXY_URL=http://34.172.70.149 `
  --dart-define=SUPABASE_URL=https://YOUR_PROJECT.supabase.co `
  --dart-define=SUPABASE_ANON_KEY=YOUR_SUPABASE_ANON_KEY `
  --dart-define=SUPABASE_REDIRECT_SCHEME=ebb `
  --dart-define=SUPABASE_REDIRECT_HOST=login-callback
```

## 5. Notes

- Existing local guest playlists, history, covers, and downloaded tracks are migrated into scoped storage automatically.
- On first sign-in, guest-scoped local data is copied into that user account's local scope if the account is still empty on the device.
- Cloud sync in Phase 1 covers profiles, playlists, playlist tracks, play events, search events, and library ownership metadata.
- Actual MP3 files remain device-local in Phase 1.
