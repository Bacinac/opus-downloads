#!/bin/bash
# Deploy OPUS · Downloads to an instance from deploy/hosts.conf (default: prod).
# The steps are opus-core's ops/deploy.sh; this file names what only Downloads has.
set -euo pipefail
cd "$(dirname "$0")/.."

OPUS_MODULE=opus-downloads
OPUS_SERVICES=(docker-controller backend engine-ui frontend)
OPUS_HEALTH_PORTS=(8097 8099)
OPUS_VOLUME_OWNER=(opus_downloads_backend all /landing /engines)

source backend/opus_core/ops/deploy.sh
opus_deploy "$@"
