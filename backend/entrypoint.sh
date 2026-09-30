#!/bin/sh
set -e
if [ "${1:-}" = "docker-controller" ]; then
    app=opus.docker_controller:app port=2376
elif [ "${1:-}" = "engine-ui" ]; then
    app=opus.engine_ui:app port=8099
else
    alembic upgrade head
    app=opus.main:app port=8097
fi
if [ "${OPUS_DEV_RELOAD:-0}" = "1" ]; then
    exec uvicorn "$app" --host 0.0.0.0 --port "$port" --reload
fi
exec uvicorn "$app" --host 0.0.0.0 --port "$port"
