# Backend deployment guide

Current host, exact URLs/build/hash, SSH identity, private paths, four Compose overlays, backup/restore limits and rollback checkpoints are owned by [OPERATIONS.md](handoff/OPERATIONS.md). AWS is serving; Oracle is stopped. Use that guide rather than historical OCI instructions as a live runbook.

## Current deployment shape

Caddy public HTTPS -> private FastAPI, recommendation worker and static web gateway, with browser-audio and PO-token services. Runtime SQLite/downloads and protected config/cookies persist outside releases. Supabase structured account data and R2 objects are external services, not included in VM database backups.

Use compose.yml, compose.web.yml, compose.cookies.yml and compose.browser-audio.yml with the existing neatie project/environment. Do not expose internal API/transport ports. Package only authorized source/build changes; preserve runtime/env mounts, readable permissions and verified rollback before promotion. Replaced bind-mounted Caddy configuration may require gateway recreation rather than reload.

Verify focused regressions, public build/hash/TLS, affected gateway contracts, actual playback/ranges and worker/state restart. Desktop/fixture success does not close iPhone or real-account gates. Do not execute deployment merely because this document describes it.

## Checked-in workflow: Oracle-targeted, configuration unverified

.github/workflows/backend-production-deploy.yml supports manual dispatch and develop pushes affecting backend/deployment paths. It currently expects:

- Variables: OCI_HOST, OCI_SSH_USER, AURALIS_API_HOST.
- Secrets: OCI_SSH_PRIVATE_KEY, OCI_KNOWN_HOSTS, BACKEND_ENV_B64; optional YTDLP_COOKIES_B64.
- AURALIS_PROXY_URL is the client production endpoint and must be verified separately.

This workflow has not been adapted/configured as the current AWS deployment mechanism. Existing repository variable/secret values were not inspected in this documentation pass. Do not assume a push or old OCI configuration deploys safely to AWS.

The existing export_backend_env.ps1/export_ytdlp_cookies.ps1 helpers curate protected configuration; their outputs contain secrets even when encoded. Keep values out of source/chat/logs, clear secret-bearing clipboards and preserve pinned host checking. Browser builds receive only public Supabase client configuration, never backend/service-role environments.

The deployment helper validates environment/identity, installs required Docker/security updates, builds/releases, checks private/public health and rolls back on failure. Confirm its target and complete six-service overlays before future use.

## Backup/restore

Daily runtime backups use SQLite's online API; individual DB consistency is not a simultaneous transaction across stores. Keep off-host copies and verify hash/path/SQLite integrity. An archive containing config/cookies/certificates is private. Refer to OPERATIONS for exact archives and exclusions.

An isolated restore test remains open for the newer AWS archive. A live restore needs a maintenance window and the same project/env/overlays, validated ownership/state and rollback; avoid old single-overlay down/up snippets. No Oracle retirement, ARM migration, new cloud instance or IP move is implied by this guide.
