#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "deploy_host.sh must run through sudo" >&2
  exit 1
fi

release_id="${1:-}"
api_host="${2:-}"
bundle_path="${3:-}"
backend_env_path="${4:-}"
cookies_path="${5:-}"

if [[ ! "$release_id" =~ ^[A-Za-z0-9._-]{7,80}$ ]]; then
  echo "Invalid release identifier" >&2
  exit 1
fi
if [[ ! "$api_host" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "Invalid API hostname" >&2
  exit 1
fi
if [[ ! -f "$bundle_path" || ! -f "$backend_env_path" ]]; then
  echo "Deployment bundle or backend environment file is missing" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl docker.io docker-compose-v2 python3 ufw unattended-upgrades
systemctl enable --now docker
systemctl enable --now unattended-upgrades

ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

install -d -m 0755 /opt/neatie/releases /opt/neatie/shared
install -d -m 0750 -o 10001 -g 10001 /var/lib/neatie/runtime
install -d -m 0750 -o 10001 -g 10001 /var/lib/neatie/downloads
install -d -m 0750 -o 10001 -g 10001 /var/lib/neatie/secrets
install -d -m 0750 /var/backups/neatie

release_dir="/opt/neatie/releases/$release_id"
rm -rf "$release_dir"
install -d -m 0755 "$release_dir"
tar -xzf "$bundle_path" --no-same-owner -C "$release_dir"

install -m 0600 "$backend_env_path" /opt/neatie/shared/backend.env
if [[ -n "$cookies_path" && -f "$cookies_path" ]]; then
  install -m 0400 -o 10001 -g 10001 "$cookies_path" /var/lib/neatie/secrets/ytdlp-cookies.txt
  printf 'AURALIS_YTDLP_COOKIES_PATH=/run/neatie-secrets/ytdlp-cookies.txt\n' >> /opt/neatie/shared/backend.env
else
  rm -f /var/lib/neatie/secrets/ytdlp-cookies.txt
fi
printf 'AURALIS_API_HOST=%s\n' "$api_host" > /opt/neatie/shared/deploy.env
chmod 0644 /opt/neatie/shared/deploy.env

compose=(docker compose --project-name neatie --env-file /opt/neatie/shared/deploy.env -f "$release_dir/deploy/production/compose.yml")
previous_release=""
if [[ -L /opt/neatie/current ]]; then
  previous_release="$(readlink -f /opt/neatie/current || true)"
fi

rollback() {
  exit_code=$?
  echo "Deployment failed; collecting bounded service diagnostics" >&2
  "${compose[@]}" ps >&2 || true
  "${compose[@]}" logs --tail 80 api recommendation-worker caddy >&2 || true
  if [[ -n "$previous_release" && -d "$previous_release" ]]; then
    previous_compose=(docker compose --project-name neatie --env-file /opt/neatie/shared/deploy.env -f "$previous_release/deploy/production/compose.yml")
    "${previous_compose[@]}" up -d --remove-orphans >&2 || true
    ln -sfn "$previous_release" /opt/neatie/current
  fi
  exit "$exit_code"
}
trap rollback ERR

python3 "$release_dir/deploy/production/runtime_backup.py" backup \
  --source /var/lib/neatie/runtime \
  --destination /var/backups/neatie \
  --retention-days 14

"${compose[@]}" build --pull
"${compose[@]}" up -d --remove-orphans

api_container="$("${compose[@]}" ps -q api)"
for _ in $(seq 1 40); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' "$api_container" 2>/dev/null || true)"
  [[ "$health" == "healthy" ]] && break
  sleep 3
done
if [[ "$(docker inspect --format '{{.State.Health.Status}}' "$api_container" 2>/dev/null || true)" != "healthy" ]]; then
  echo "API container did not become healthy" >&2
  false
fi

worker_container="$("${compose[@]}" ps -q recommendation-worker)"
sleep 5
worker_running="$(docker inspect --format '{{.State.Running}}' "$worker_container" 2>/dev/null || true)"
worker_restarts="$(docker inspect --format '{{.RestartCount}}' "$worker_container" 2>/dev/null || echo 999)"
if [[ "$worker_running" != "true" || "$worker_restarts" -gt 2 ]]; then
  echo "Recommendation worker is not stable" >&2
  false
fi

for _ in $(seq 1 40); do
  if curl --fail --silent --show-error --max-time 10 "https://$api_host/" | grep -qi running; then
    break
  fi
  sleep 3
done
if ! curl --fail --silent --show-error --max-time 10 "https://$api_host/" | grep -qi running; then
  echo "Public HTTPS health check failed" >&2
  false
fi

ln -sfn "$release_dir" /opt/neatie/current
install -m 0644 "$release_dir/deploy/production/neatie-backup.service" /etc/systemd/system/neatie-backup.service
install -m 0644 "$release_dir/deploy/production/neatie-backup.timer" /etc/systemd/system/neatie-backup.timer
systemctl daemon-reload
systemctl enable --now neatie-backup.timer

trap - ERR
rm -f "$bundle_path" "$backend_env_path"
[[ -n "$cookies_path" ]] && rm -f "$cookies_path"

find /opt/neatie/releases -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
  | sort -rn \
  | tail -n +4 \
  | cut -d' ' -f2- \
  | while IFS= read -r old_release; do
      [[ "$old_release" == "$release_dir" || "$old_release" == "$previous_release" ]] || rm -rf "$old_release"
    done

echo "Neatie deployment is healthy at https://$api_host"
