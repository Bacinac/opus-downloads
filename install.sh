#!/usr/bin/env bash
# Install OPUS Downloads after OPUS Library has supplied its URL, the token it
# issued to Downloads and the shared session key. Existing secrets and data are
# preserved on reruns.
set -euo pipefail
cd "$(dirname "$0")"

source backend/opus_core/ops/install.sh

START=1
for arg in "$@"; do
	case "$arg" in
		--no-start) START=0 ;;
		-h|--help) opus_help; exit 0 ;;
		*) echo "unknown option: $arg" >&2; exit 2 ;;
	esac
done

opus_require docker python3 curl
opus_env_file
opus_required "provided by OPUS Library" OPUS_AUTH_URL OPUS_AUTH_TOKEN OPUS_SESSION_KEY
opus_generate POSTGRES_PASSWORD:24

ROOT=$(pwd -P)
HOST=$(opus_host_address)
mkdir -p "$ROOT/volumes/landing" "$ROOT/volumes/engines" "$ROOT/volumes/postgres"
opus_production
env_set OPUS_DOCKER_GID "$(stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)"
env_set OPUS_ENGINES_HOST_DIR "$ROOT/volumes/engines"
LANDING=${OPUS_LANDING_DEVICE:-$(env_read OPUS_LANDING_DEVICE)}
LANDING=${LANDING:-$ROOT/volumes/landing}
env_set OPUS_LANDING_DEVICE "$LANDING"
env_set OPUS_LANDING_HOST_DIR "$LANDING"
[[ -n "$(env_read OPUS_ENGINES_URL)" ]] || env_set OPUS_ENGINES_URL "http://$HOST:8099"
opus_pass_through VITE_OPUS_LIBRARY_URL VITE_OPUS_PLAYER_URL OPUS_COOKIE_DOMAIN OPUS_MEDIA_HOST_DIR

opus_validate "OPUS Downloads"
[[ $START == 1 ]] || exit 0
[[ -S /var/run/docker.sock ]] || { echo "/var/run/docker.sock is required for engine management" >&2; exit 1; }

opus_build backend frontend
compose up -d postgres docker-controller backend engine-ui frontend
opus_wait http://127.0.0.1:8097/api/ready 120 "Downloads API"
opus_wait http://127.0.0.1:5282/ 60 "Downloads UI"
echo "OPUS Downloads is ready: http://$HOST:5282"
