#!/bin/sh
set -eu

# Copy the application into the persistent /app volume.
# Runtime files already present in /app are deliberately preserved.
if [ ! -f /app/document_watcher.py ]; then
    cp -a /source/. /app/
fi

cd /app

if [ "${DOCUMENT_RUN_MODE:-poll}" = "mqtt" ]; then
    exec python mqtt_trigger.py
fi

exec python document_watcher.py "$@"
