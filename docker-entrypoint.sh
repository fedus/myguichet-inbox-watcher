#!/bin/sh
set -eu

# Copy the application into the persistent /app volume.
# Runtime files already present in /app are deliberately preserved.
if [ ! -f /app/myguichet_get_new_messages.py ]; then
    cp -a /source/. /app/
fi

cd /app

if [ "${MYGUICHET_RUN_MODE:-poll}" = "mqtt" ]; then
    exec python mqtt_trigger.py
fi

exec python myguichet_get_new_messages.py "$@"
