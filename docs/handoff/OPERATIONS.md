# Operations

Reconciled October 4, 2026. AWS is the serving host; Oracle is stopped. This file owns operational paths, checksums, backups, rollback and historical hosting constraints. [CURRENT_STATUS.md](CURRENT_STATUS.md) owns application verification; [NEXT_STEPS.md](NEXT_STEPS.md) owns pending O01–O06. Paths only: never record credential/cookie/key values.

## Current serving host

| Field | Value |
| --- | --- |
| Instance | neatie-aws-1, AWS Sydney ap-southeast-2 |
| Shape / OS | m7i-flex.large, 2 vCPUs / 8 GiB, Ubuntu 24.04.4 x86_64 |
| Disk | approximately 40 GiB gp3 |
| IPv4 | 13.236.9.131; Elastic IP association unverified |
| Web | https://preview.13.236.9.131.sslip.io/ |
| API | https://13-236-9-131.sslip.io/ |
| Build | 2026-10-05-editorial |
| Public main.dart.js SHA-256 | 580d9058a3bd68bb80ec0b5614ccf2f6948e2ef75a20149ad44187acafc1a893 |
| Release / symlink | /opt/neatie/releases/aws-20261003-oct1 / /opt/neatie/current |
| Environments | /opt/neatie/shared/backend.env and /opt/neatie/shared/deploy.env |
| Runtime / downloads | /var/lib/neatie/runtime / /var/lib/neatie/downloads |

Public ports are 80/443; SSH host UFW allows 22 and the user's EC2 security group supplies source restrictions. Internal API/token/browser-audio ports are not publicly bound. Inner web gateway binds 127.0.0.1:8081 -> 8080. No AWS API/console change or paid-plan conversion was performed by the agent.

A reboot ordinarily preserves an automatically assigned public IP; stop/start may not. Before resizing/stopping, verify stable IP or plan bundle-origin/API/Supabase callback changes. Current displayed compute rate was $0.1197/hour (~$87.38/730 hours), before disk/IPv4: $100 is finite credit, not six months of this VM. About 2.6 GiB host use was observed during playback/worker activity. A 4 GiB instance is a test candidate; 2 GiB fit is not established. Offer/pricing research must be refreshed before a future decision.

## Worktrees and private local files

October 5 quiet minimal release: local stage C:/Users/ADMIN/AppData/Local/Temp/neatie-editorial-20261005; remote /home/ubuntu/neatie-editorial-20261005. `editorial.tgz` SHA-256 is 348b8bf887c20b4e0ca0087871e5dad122332cef18d7ddc8ee8397c36117327c. The manifest has 56 frontend source/assets/bundle hashes; tests, runtime, docs and private files were excluded. Promotion completed 2026-10-05T01:52:48Z after an online runtime backup. Only web-preview was recreated; API/worker uptimes were preserved and all six services run. Public HTTPS main.dart.js matches the current table hash.

Rollback retains the previous 2026-10-04-home-presentation bundle at `/opt/neatie/releases/aws-20261003-oct1/app/build/web.pre-editorial-20261005`, prior frontend source at `/var/backups/neatie/pre-editorial-20261005/source.tgz`, and runtime backup at `/var/backups/neatie/pre-editorial-20261005/runtime/neatie-runtime-20261005T014553Z.tar.gz`. Source tag `neatie-web-before-editorial-20261005` points to f94103d. On the VM, run:

```bash
sudo bash /var/backups/neatie/pre-editorial-20261005/rollback-web.sh
```

The script checks the expected current/previous bundle hashes and release path, preserves the withdrawn bundle, restores prior frontend source/static assets, recreates only web-preview and verifies the old public hash. It leaves current runtime data untouched. It was syntax checked; an actual rollback was not performed. Reassess the guard before using it after a later release.

Main/Android: C:/Users/ADMIN/Documents/GitHub/personal-music-player, develop, committed e720709.
Web/backend source: C:/Users/ADMIN/Documents/GitHub/personal-music-player-web, codex/web-app, application checkpoint e4e7529. H01–H14, album transport and the quiet minimal frontend are committed; tests/runtime/private files remain excluded. The October 4 hash manifests identify their targeted releases independently of later commits. H09–H14 and the October 5 redesign are web-only releases; no API/worker rebuild or restart, runtime replacement or cloud-plan change.

Main-only unreleased playlist files: app/lib/logic/playlist_provider.dart, playlist_sync_delta.dart and app/test/playlist_sync_delta_test.dart. Neither branch HEAD captures all current work; choose the owning tree and preserve unrelated changes.

| Private/local path | Purpose |
| --- | --- |
| C:/Users/ADMIN/Documents/NeatieSecrets/neatie-aws.pem | AWS SSH private key; restricted Windows owner permissions |
| C:/Users/ADMIN/Documents/NeatieSecrets/neatie-aws-known-hosts | Pinned AWS SSH host identity |
| C:/Users/ADMIN/Documents/NeatieSecrets/ssh-key-2026-08-21.key | Stopped Oracle private SSH key |
| Same path plus .pub | Oracle public key, not the private key |
| C:/Users/ADMIN/Documents/NeatieSecrets/www/.youtube.com/_cookies(1).txt | Original supplied cookie file; age/validity not guaranteed |
| C:/Users/ADMIN/Documents/NeatieSecrets/Backups/ | Private off-host archives and verification reports |
| Each worktree's .env | Protected local config; do not compile it wholesale into web |
| C:/Users/ADMIN/AppData/Local/Temp/neatie-web-public-config.json | Temporary browser build config: ONLY public SUPABASE_URL/SUPABASE_ANON_KEY |
| C:/Users/ADMIN/flutter/bin/cache/dart-sdk/bin/dart.exe | Cached Dart executable |
| C:/Users/ADMIN/flutter/bin/cache/flutter_tools.snapshot | Direct Flutter entry point avoiding failing Git auto-fetch |
| C:/Users/ADMIN/AppData/Local/Programs/Python/Python311/python.exe | Verification Python |

AWS SSH (read/write scope must match the authorized task):

```powershell
ssh -i "C:/Users/ADMIN/Documents/NeatieSecrets/neatie-aws.pem" -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="C:/Users/ADMIN/Documents/NeatieSecrets/neatie-aws-known-hosts" ubuntu@13.236.9.131
```

SSH timeout alone does not establish host loss. The user's public IPv4 /32 changes; a local 192.168.x.x address is not an inbound source replacement. Do not disable pinned host checking to bypass a changed identity.

## Services and deployment boundaries

Services: neatie-api-1, neatie-recommendation-worker-1, neatie-browser-audio-1, neatie-pot-provider-1, neatie-web-preview-1, neatie-caddy-1.

Compose project neatie; working directory /opt/neatie/current/deploy/production. Use sudo and all four overlays: compose.yml + compose.web.yml + compose.cookies.yml + compose.browser-audio.yml, deployment env /opt/neatie/shared/deploy.env. Protected backend env is not a public build config.

Caddyfile.preview is outer HTTPS; Caddyfile.web is inner static/API gateway. Named Caddy data/config volumes preserve certificates. Live cookies mount: /var/lib/neatie/secrets/youtube-cookies.txt. The gateway forwards existing search/related/prepare/playback/recommendation/detail contracts and POST search_interaction, interaction_event plus history_seed. Do not expose arbitrary administration routes.

October 4 album transport audit reproduced yt-dlp save-back to the read-only cookie mount, masked as stream_failed, and missing worker token-provider/browser-audio settings. The subsequent correction is deployed: preserve the master mount as read-only, load a fresh extraction-owned in-memory cookie jar without save-back, align AURALIS_YTDLP_JS_RUNTIME/AURALIS_YTDLP_PO_PROVIDER_URL and healthy dependencies, and supply existing AURALIS_BROWSER_AUDIO_URL to both API and worker. Recommendation verification passes background=True to retain existing bounded capture priority. Missing worker JS-runtime had defaulted to the present node binary; it was not independently the failing boundary. The deployed worker's direct extraction/media preflight succeeded in 7.34 seconds with master cookies unchanged. Full album publication/open/play remains unaccepted; see KNOWN_ISSUES and H04.

Replacing a bind-mounted Caddyfile via tar can leave the container on the old inode: September 26 reload alone retained old routes. Recreate the affected gateway to remount a replaced config; verify routes. September 28 initial packaging was owner-only/unreadable by the non-root service user; rollback succeeded, a+rX staged source fixed startup. Preserve readable/traversable permissions.

The checked-in backend-production-deploy workflow is Oracle-targeted; it is not automatically configured for this AWS host. Do not copy obsolete OCI variables or dispatch deployment as part of documentation work. Keep state/cookies/config outside source releases and preserve mounts during upgrades.

## AWS restore and release integrity

September 20 archive was restored on AWS October 3. Only runtime SQLite, backend env and cookies were restored; latest local source, browser-audio, web/gateway were deployed separately. Supabase/R2 were reused; September 21 onward Oracle runtime was not retrieved. Both assistant_memory.sqlite and recommendation_store.sqlite passed quick_check before startup. API/worker restart retained state.

Local stage C:/Users/ADMIN/AppData/Local/Temp/neatie-aws-20261003; remote /home/ubuntu/neatie-aws-20261003. Diagnostics/staging paths are not source authority.

| Release artifact | SHA-256 |
| --- | --- |
| Initial AWS source upload | a3256a9dd5252048e49733eff9742176f2fc24ead852251b7c5feb38187eed7d |
| Initial AWS October 1 web bundle (superseded) | fcbfb0ef30d19deca45bed9f63f3b3e4f86742d68790b905b970b087ff757bc4 |
| October 3 history/Search first targeted upload | cfc85c9f5fe0a140ecc9173a24c3a9e07ced3de4898ef2eecae6d898ca399713 |
| October 3 history/Search bundle (superseded) | 3924d5579a73175f8c97d683fabbe31ede05f2c51bafd7a31f057cdae8025a92 |
| October 4 H01–H04 targeted upload | b68e8ad81299b0562ed9d4b5990226f1ce5af180cb8fce3be15d0af5479f8667 |
| October 4 H01–H04 web bundle (superseded) | 2283cf0d3f7b56586eb22433df2c9e04ae8411d001239a903ba3b2af81c46789 |
| October 4 H05–H08 targeted upload | a83d732d2f64dd0a285591bbdf97acd89163227264e771833573461ca1946928 |
| October 4 H05–H08 web bundle (superseded) | d2cf10bc1351f9932f3d2bd7cc336eef754932dca57ccc9da457bf309070277d |
| October 4 H09–H14 targeted upload | 6dd85d9b3d911a5c287dbf3bc888c77264d7fd696997c6589662d864d1fe20e2 |
| October 4 H09–H14 web bundle (retained for rollback) | 306ffac03c6f9281474bedc02077c7a5aa77e9f3c312a9de2d69441b7227d204 |
| Current October 5 quiet minimal web bundle | 580d9058a3bd68bb80ec0b5614ccf2f6948e2ef75a20149ad44187acafc1a893 |

October 4 stage: C:/Users/ADMIN/AppData/Local/Temp/neatie-aws-20261004 and /home/ubuntu/neatie-aws-20261004. home-artwork.tgz contains nine backend modules, five web source files and the release web bundle; tests/docs/private files were excluded. manifest.json records all 15 source/bundle hashes, verified after remote extraction. Compilation used only public Supabase configuration plus the existing AWS API origin. Deployment retained runtime/config/cookie/certificate mounts and used all four overlays. A guarded deployment script restores prior code/web/images on failure; no rollback was needed. All six services and public HTTPS/hash were verified. H05–H08 are not included in this artifact.

H05–H08 deployed stage: C:/Users/ADMIN/AppData/Local/Temp/neatie-aws-20261004-radio and /home/ubuntu/neatie-aws-20261004-radio. album-radio.tgz/hash and bundle/hash are recorded above; label `2026-10-04-album-radio`. Explicit archive allowlist contains six backend modules, web_app.dart and generated web output; no tests/docs/private files. Eight manifest hashes matched after extraction. The guarded script retained pre-album-radio-20261004 source/web/images and online runtime backup, rebuilt API/worker with all four overlays and recreated web-preview. No rollback was needed; public build/hash/origin, six running services and configured health checks passed. Non-sensitive aggregate supply/freshness audits ran under user authorization; sensitive access still requires permission. No post-deploy account artifact had been generated at audit time; queued work retains backoff.

SSH recovery: the user updated only port 22's source to My IP in neatie-web. AWS's My IP fills the current local network's public IPv4 /32; repeat when that address changes. The server's 13.236.9.131 address was unchanged. HTTP/HTTPS rules were retained.

### AWS rollback checkpoints

- October 4 album transport: /var/backups/neatie/pre-album-transport-20261004/code.tgz; images neatie-api:pre-album-transport-20261004 and neatie-recommendation-worker:pre-album-transport-20261004. Online SQLite backup /var/backups/neatie/pre-album-transport-20261004/runtime/neatie-runtime-20261004T072112Z.tar.gz; backup/quick checks passed, not off-host/full-restore tested.
- Local stage C:/Users/ADMIN/AppData/Local/Temp/neatie-aws-20261004-album-transport; remote /home/ubuntu/neatie-aws-20261004-album-transport. Explicit archive allowlist contains stream_core_runtime.py, source_registry.py and compose.yml/compose.browser-audio.yml only; no tests/docs/private files. album-transport.tgz SHA-256 81aadaedc0311beaa25ae933cf8309bc7fe9c17551c5f8daf347d8d15fda55cc; all four manifest hashes matched. Guarded deployment rebuilt/recreated API/worker with all four overlays, preserving master cookies, runtime, auth/origins and web bundle. All services/health checks, merged worker/API transport parity and public HTTPS passed. No rollback was needed. Completed 2026-10-04T07:22:20Z; web build/hash remain the H09–H14 values above.

- October 4 H09–H14: /var/backups/neatie/pre-home-presentation-20261004/code-web.tgz and missing-source.json; previous static directory /opt/neatie/releases/aws-20261003-oct1/app/build/web.pre-home-presentation-20261004. Guarded deployment restores the old web/source on failure and recreates web-preview with all four overlays. No image tags needed because API/worker were unchanged.
- Consistent pre-H09–H14 runtime: /var/backups/neatie/pre-home-presentation-20261004/runtime/neatie-runtime-20261004T064446Z.tar.gz. Online SQLite backup/quick checks passed; not downloaded or full-restore tested. Local/remote stage: C:/Users/ADMIN/AppData/Local/Temp/neatie-aws-20261004-home and /home/ubuntu/neatie-aws-20261004-home. Archive allowlist includes six web sources and compiled output; no tests/docs/private files. Seven source/bundle hashes and public HTTPS/build/origin passed; only web-preview was recreated. No rollback was needed. Completed 06:45:18 UTC.

- October 4 H05–H08: /var/backups/neatie/pre-album-radio-20261004/code-web.tgz, images neatie-api:pre-album-radio-20261004 and neatie-recommendation-worker:pre-album-radio-20261004.
- Consistent pre-H05–H08 runtime: /var/backups/neatie/pre-album-radio-20261004/runtime/neatie-runtime-20261004T052207Z.tar.gz. Online SQLite backup/quick checks passed; not downloaded or full-restore tested.
- October 4: /var/backups/neatie/pre-home-artwork-20261004/code-web.tgz, images neatie-api:pre-home-artwork-20261004 and neatie-recommendation-worker:pre-home-artwork-20261004.
- Consistent pre-deploy runtime: /var/backups/neatie/pre-home-artwork-20261004/runtime/neatie-runtime-20261004T040955Z.tar.gz. SQLite online backup/quick checks passed; this archive has not been downloaded or full-restore tested.

- /var/backups/neatie/pre-history-search-20261003/code-web.tgz
- /var/backups/neatie/pre-initial-retry-20261003/api.py
- /var/backups/neatie/pre-mix-reservoir-20261003/ranking.py
- /var/backups/neatie/pre-history-admission-20261003/{admission.py,bundles.py}

Matching images neatie-api:pre-<stage>-20261003 and neatie-recommendation-worker:pre-<stage>-20261003, stage history-search, initial-retry, mix-reservoir or history-admission. API/worker were rebuilt with existing overlays and web gateway recreated; mounts/env retained. No runtime replacement or cloud-plan change was performed in that follow-up.

## Backups and restore limits

Daily consistent runtime timer is active. Existing runtime_backup.py uses SQLite online backup; individual databases are consistent, not one simultaneous transaction across all stores. On-host retention alone is insufficient for host loss. Export off-host after later changes; archives include credentials/cookies/certificate private data, so keep outside Git/private. Encryption at rest was not claimed.

### Newer AWS archive — integrity verified, full restore untested

- Server /var/backups/neatie/neatie-aws-migration-20261003T121109Z.tar.gz; downloaded C:/Users/ADMIN/Documents/NeatieSecrets/Backups/neatie-aws-migration-20261003T121109Z.tar.gz.
- 92,477,791 bytes; SHA-256 6903b8ef70d10b2152665ceb5f498bcc4b93b7240cd0c8b59c59c9c220039dd3.
- Local/server hashes match; 272 archive paths validated; both extracted SQLite databases passed quick_check. Report is the archive path plus .verification.json.
- Includes runtime snapshots, current release/config, cookies/downloads and Caddy volumes. Excludes external Supabase/R2, Docker images and OS.
- Predates the latest history/Search fixes. Source/image checkpoints above preserve preceding releases. Full restoration of this archive is not tested.

### September 20 Oracle archive — used for AWS restoration

- Local C:/Users/ADMIN/Documents/NeatieSecrets/Backups/neatie-migration-20260920T084746Z.tar.gz and same path plus .verification.json.
- Server /var/backups/neatie/neatie-migration-20260920T084746Z.tar.gz; private transfer /home/ubuntu/neatie-backup-transfer/neatie-migration-20260920T084746Z.tar.gz.
- Created 08:47:46 UTC, 92,790,406 bytes, 410 members; SHA-256 4468c2d93ebf26d826a94642982ffc18b4388755fee723f4882005c08af4c6f3. Local/server hash and two SQLite quick_checks passed.
- Contents snapshot/var/lib/neatie; opt/neatie release/config/web; snapshot/deployed-api.tar and deployed-browser.tar from running containers; docker-volumes/neatie_caddy_data and neatie_caddy_config; snapshot/manifest.json.
- Excludes external Supabase/remote objects, full OS/image export, PC-only files and later Oracle activity. The x86 AWS restore was tested; ARM compatibility and complete recovery of later Oracle activity were not.

### Restore procedure boundaries

Use an isolated restore test for O05 before replacing live data. The helper validates paths/databases and retains pre-restore state for rollback. A planned production restore requires a maintenance window, stopping API/worker with the same environment/overlays, validating restored ownership/config, then restarting the complete stack. Do not run an old one-overlay down/up snippet against the current six-service deployment.

Cloud auth/library tables library_tracks, playlists, playlist_tracks and R2 bytes are external to VM archives; reused cloud services are not proven independently backed up by these files.

## Authentication and origins

User confirmed AWS redirects saved:
- https://preview.13.236.9.131.sslip.io
- https://preview.13.236.9.131.sslip.io/**

Retain Android ebb://login-callback and ebb://login-callback/. Older Oracle preview redirects may remain for historical/device compatibility. Add new exact origin/callbacks before testing any hostname move. New browser origin requires fresh login; old-origin local guest history/queue/session data does not migrate automatically.

Email/Google enabled; GitHub remains hidden/disabled unless explicitly configured. Public browser SUPABASE_ANON_KEY is distinct from backend/service-role secrets. Ordinary login is observed, full recovery/Google/isolation acceptance remains in A01–A04. WEB_PLAYLIST_WRITES=false.

## Stopped Oracle and historical recovery paths

Former VM neatie-api-1, IPv4 168.107.91.109/private 10.0.0.145, VM.Standard.E5.Flex, 1 OCPU/8 GB, x86_64 Ubuntu 24.04, Singapore AD-1/FD-3, neatie-vcn/public subnet. Original image Canonical-Ubuntu-24.04-2026.06.29-0. Last pre-stop check September 23: API/browser/token healthy, worker/Caddy/web running. It is not the current serving host.

Account-specific email said trial expired September 20 01:41 UTC and data retrievable through October 19 01:41 UTC / 08:41 Vietnam. This is not a guaranteed E5 uptime SLA. Stopped disk may retain September 21 onward data; recovery is O06, not authorization to restart/delete.

A1 attempted name neatie-free-1, Ubuntu 24.04 ARM (earlier planned image build 2026.08.25-0; later screenshots differ), 2 OCPU/12 GB and 1 OCPU/6 GB attempts both unavailable in offered AD-1. Earlier quota usage 0/2 cores and 0/12 GB was not physical capacity. No allocated IP or saved stack was confirmed; a stack does not reserve capacity or auto-retry. Old boot UI default 46.6 GB, later custom 50 GB/10 VPU screenshots; storage estimates are not blanket proof of zero billing. Recheck actual tenancy allowance/total retained volumes for a future move; ARM images/native dependencies are untested.

Historical Oracle source symlink /opt/neatie/current -> /opt/neatie/releases/web-preview-20260911 (directory date not serving build date); same runtime/env/cookie/Caddy-volume conventions as above. Old endpoints https://preview.168.107.91.109.sslip.io/ and https://168-107-91-109.sslip.io/ are historical. No preview passkey was required.

| Historical rollback/stage | Retained location / evidence |
| --- | --- |
| September 18 playback | /var/backups/neatie/pre-playback-validation-20260918.tgz; neatie-api:pre-playback-validation-20260918 |
| September 20 web | /var/backups/neatie/pre-web-client-20260920.tgz; web hash 443f55568a7577c386bc72275b90ba158c7bb399301be73aa411a96e9d705f73 |
| September 24 catalog/Home | /var/backups/neatie/pre-catalog-web-20260924-verified.tgz; neatie-api:pre-home-fix-20260924 and worker equivalent; web hash e6240851c99a7cf298307f99cd65f8c456b023d7144236ee0cf800f7165dcad7 |
| September 26 reliability | /var/backups/neatie/pre-reliability-20260926/web.tgz plus backend copies; neatie-api:pre-reliability-20260926 and worker equivalent; uploaded SHA bf8974a31d6dba5e17c0f413e76f350abac1740ac8454f74e4570f91fff3a17a; web hash aad0d76e26437d9ddc2e0d09eb86f9bf7b959604374d674dacdd0f8090954b28 |
| September 28 Home/Search | /var/backups/neatie/pre-home-search-20260927/web.tgz plus code/api.log/worker.log; neatie-api:pre-home-search-20260927 and worker equivalent; web hash 7071d704f840a0b3c7586bc6c55aa1e60ef47c31546d96087b5fd6dc08cc7b1a |
| September 29 guest | /var/backups/neatie/pre-home-guest-20260929; neatie-api:pre-home-guest-20260929 and worker equivalent; web hash 912db6b319055747d380eafbfdd9cec645c569fa8c6d87be384eea9b92701b60 |

Historical local stages under C:/Users/ADMIN/AppData/Local/Temp: neatie-reliability-20260926 (script neatie-reliability-deploy.sh), neatie-home-search-20260927 (neatie-home-search-deploy.sh), neatie-home-guest-20260929 (deployment script in directory). Remote /home/ubuntu has matching stage directory names. Paths are recorded, not newly checked or guaranteed retained forever.

## Hosting research retained as dated leads, not current offers

October 3 research preceded AWS deployment. User has university email, Azure student verification failed and no continuously powered spare PC. No option was verified to provide permanent reliable free hosting of the complete stack. Do not create accounts, convert plans or rotate identities to bypass eligibility as part of documentation.

The API/worker share live SQLite and should stay together; managed Postgres/object storage is not a drop-in filesystem replacement. Browser audio needs Chromium/Playwright/ffmpeg plus Node PO provider and outbound network. A configured 2 GB browser ceiling/512 MB shared memory is not measured minimum use. Static Pages hosting can move only the frontend; separate origin requires CORS/artwork/gateway/Supabase checks. Object storage is for bounded bytes/backups, never mounted live SQLite.

| Lead / dated constraint | Evidence and remaining condition |
| --- | --- |
| Azure Students | Previously advertised $100/year and first-year 750-hour B1s/B2pts_v2/B2ats_v2 allowances; annual credit renewal is not compute-allowance renewal. B2ats_v2 is 1 GiB/burst CPU. Signup failed for user; eligibility, outbound IP/disk cost and exact credit protection require recheck. [Eligibility](https://learn.microsoft.com/en-us/azure/education-hub/about-azure-for-students), [service allowances](https://azure.microsoft.com/en-us/free/students/), [sizes](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/basv2-series), [FAQ](https://learn.microsoft.com/en-us/azure/education-hub/faq) |
| Oracle E2 Micro / A1 | Two E2 Micro nodes were a potential ongoing allowance, each 1 GB / 1/8 OCPU; capacity/performance/reclamation unknown. Prior 200 GB combined-volume guidance must include retained disks. A1 attempts failed. [Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), [expiry policy](https://docs.oracle.com/en-us/iaas/Content/GSG/Tasks/signingup_topic-What_Happens_When_the_Promotion_Expires.htm) |
| Azure startup | Dated no-investor offer $1,000/90 days, possible $4,000/180 days after business verification. Genuine business/new-customer eligibility required; subscription expiry/payment behavior varies. [FAQ](https://learn.microsoft.com/en-us/startups/microsoft-for-startups/mfs-faqs), [activation](https://learn.microsoft.com/en-us/startups/build/azure-getting-started/activate) |
| AWS Free Plan | User screenshot showed $100/183 days, expiry April 3, 2027; credit may exhaust earlier. Dated offer up to another $100 through specified activities, no automatic six-month hosting guarantee. No paid upgrade was performed. [FAQ](https://aws.amazon.com/free/free-tier-faqs/) |
| GitHub Education / Heroku | Dated $13/month for 24 months, no rollover/renewal; Basic $7/512 MB, separate ephemeral dyno filesystems conflict with shared SQLite. Card verification required; old signup text differed. [Program](https://help.heroku.com/Z3RHNRHD/how-does-the-heroku-for-github-students-program-work), [sizes](https://devcenter.heroku.com/articles/dyno-sizes), [dynos](https://devcenter.heroku.com/articles/dynos), [signup](https://help.heroku.com/MV88YDXQ/how-do-i-sign-up-for-the-heroku-github-student-program), [Education](https://github.com/education/students), [Pack](https://education.github.com/pack) |
| Northflank | Dated sandbox two services/database/two cron jobs; precise free RAM/volumes/audio egress and zero-spend protection unresolved; payment method required, not production assurance. [Pricing](https://northflank.com/pricing), [billing](https://northflank.com/docs/v1/application/billing/pricing-on-northflank) |
| University VM | Possible institution-approved 1–2 vCPU/4–8 GB/40–50 GB Linux project VM with Docker, public HTTPS and outbound access; proposed resources, no entitlement/allocation established |
| Pages/R2 components | Dated Pages 500 builds/month, 20,000 files, 25 MiB/file; R2 Standard 10 GB-month/1m Class A/10m Class B and direct egress allowance; overage billable. [Pages](https://developers.cloudflare.com/pages/platform/limits/), [R2](https://developers.cloudflare.com/r2/pricing/). Audio through Tunnel is subject to [delivery terms](https://developers.cloudflare.com/fundamentals/reference/policies-compliances/delivering-videos-with-cloudflare/), not unlimited free media transport |
| Poor direct migrations | Render sleep/no persistent free disk/expiring free DB; Koyeb 512 MB/0.1 vCPU/no worker-volume/idle sleep; Fly two runtime hours/seven days; Railway $1 monthly after trial; new Hugging Face compute creation/payment constraints. [Render](https://render.com/docs/free), [Koyeb](https://www.koyeb.com/docs/reference/instances), [Fly](https://docs.fly.io/about/free-trial), [Railway](https://blog.railway.com/p/free-plan), [Spaces](https://huggingface.co/docs/hub/en/spaces-overview) |

Split-host proposals need real resident memory/CPU/capture/concurrency/egress tests on each node (RAM is not pooled), private service networking and persistent restart/worker progress/device acceptance. Azure outbound defaults/IP charges may need explicit routing; do not add costly NAT merely to obtain outbound access. [Outbound](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access), [IP pricing](https://azure.microsoft.com/en-us/pricing/details/ip-addresses/).

Historical engineering estimates excluding allocation/signup: conventional x86 restore about one working day (now completed on AWS); split setup/profiling 2–5 days, subject to provider/device failures. Old Pack articles promising DigitalOcean were not supported by the researched current listing. Recheck all offers before spending time or money.
