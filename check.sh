#!/bin/sh
# Everything this repository checks about itself, in one command. The check is
# the same for every OPUS module and lives in opus-core.
set -eu
cd "$(dirname "$0")"
. backend/opus_core/ops/check.sh
opus_check
