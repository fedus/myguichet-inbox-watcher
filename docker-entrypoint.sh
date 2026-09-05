#!/bin/sh
set -eu

if [ "${DOCUMENT_RUN_MODE:-poll}" = "mqtt" ]; then
    exec python mqtt_trigger.py "$@"
fi

exec python document_watcher.py "$@"
