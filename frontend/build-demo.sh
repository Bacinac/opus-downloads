#!/usr/bin/env bash
# Build the backend-less OPUS Downloads public demo.
set -euo pipefail

UI="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$UI/demo-dist}"
cd "$UI"
OPUS_DEMO=1 VITE_OPUS_DEMO=1 npm run build
rm -rf "$OUT"
mv build "$OUT"
. ../backend/opus_core/ops/revision.sh
opus_revision HEAD false > "$OUT/demo-version.json"
printf '/*  /index.html  200\n' > "$OUT/_redirects"
echo "OPUS Downloads demo built -> $OUT"
