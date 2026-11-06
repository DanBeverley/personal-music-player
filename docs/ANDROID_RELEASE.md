# Android Release and Self-Update

Neatie publishes signed split APKs and `update.json` to a public GitHub
release. The Android app checks the latest manifest at launch at most once per
24 hours, selects the APK matching the device ABI, verifies its size and
SHA-256, and then opens Android's package installer.

## GitHub configuration

Repository secrets:

- `ANDROID_KEYSTORE_BASE64`
- `ANDROID_KEY_ALIAS`
- `ANDROID_STORE_PASSWORD`
- `ANDROID_KEY_PASSWORD`

Repository variables:

- `AURALIS_PROXY_URL` — the public HTTPS base URL of the production backend
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_REDIRECT_SCHEME`
- `SUPABASE_REDIRECT_HOST`

The workflow deliberately fails when any value is absent. Secrets must never
be placed in source files, workflow YAML, release notes, or chat logs.

## Publish a release

1. Set `version:` in `app/pubspec.yaml` to a new version name and a strictly
   higher Android build number, for example `1.0.1+2`.
2. Commit and push the release-ready code.
3. Create and push the matching tag, for example `v1.0.1`.
4. The `Android release` GitHub Actions workflow builds and verifies all three
   APKs, generates `update.json`, and creates the public GitHub release.
5. Confirm that the release contains `update.json` and the arm64-v8a,
   armeabi-v7a, and x86_64 APKs.
6. Install the release APK on a device and verify update check, download,
   unknown-app permission, and package-installer handoff before announcing it.

The release script rejects a tag that does not match `versionName` and rejects
an APK whose signing certificate does not match the established Neatie
certificate. Keep an offline backup of the keystore; losing it prevents future
versions from updating existing installations.

## Current release gate

Current runtime host/endpoint is recorded in [OPERATIONS.md](handoff/OPERATIONS.md); current status and G05 device acceptance are in [CURRENT_STATUS.md](handoff/CURRENT_STATUS.md) and [NEXT_STEPS.md](handoff/NEXT_STEPS.md). The serving API is https://13-236-9-131.sslip.io/, but stable IP association and actual GitHub variable configuration remain unverified.

AURALIS_PROXY_URL must be the verified public HTTPS backend selected for the release, not localhost, a private IP or temporary tunnel. The workflow fails if required configuration is missing; do not infer that it remains blocked or that current repository values are correct without checking. Signed self-update still needs actual-device ABI/hash/certificate/permission/installer tests. Documentation cleanup does not authorize a tag, release, push or APK publication.
