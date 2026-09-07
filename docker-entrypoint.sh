#!/bin/sh
set -eu

if [ "${DOCUMENT_RUN_MODE:-poll}" = "mqtt" ]; then
    exec python mqtt_trigger.py "$@"
fi

if [ "${DOCUMENT_RUN_MODE:-poll}" = "api" ]; then
    exec python api_server.py "$@"
fi

if [ "${DOCUMENT_RUN_MODE:-poll}" = "watcher" ]; then
    exec python watcher_service.py "$@"
fi

exec python document_watcher.py "$@"
