#!/bin/sh
set -e
case "$OPUS_ROLE" in
    production|development) ;;
    *) echo "OPUS_ROLE must be production or development, not '$OPUS_ROLE'" >&2; exit 1 ;;
esac
if [ "${1:-}" = "docker-controller" ]; then
    app=opus.docker_controller:app port=2376
elif [ "${1:-}" = "engine-ui" ]; then
    app=opus.engine_ui:app port=8099
else
    alembic upgrade head
    python -m opus.claims
    app=opus.main:app port=8097
fi
if [ "$OPUS_ROLE" = development ]; then
    exec uvicorn "$app" --host 0.0.0.0 --port "$port" --reload
fi
exec uvicorn "$app" --host 0.0.0.0 --port "$port"
