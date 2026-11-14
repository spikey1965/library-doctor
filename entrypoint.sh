#!/bin/sh
# Start as root only long enough to make /config belong to PUID:PGID, then run Library Doctor as that user.
# If the container was started with "user:" (not root), just run it.
set -e
if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-1000}"; PGID="${PGID:-1000}"
  mkdir -p /config
  chown -R "$PUID:$PGID" /config
  exec su-exec "$PUID:$PGID" python3 -u /app/ldoctor.py
fi
exec python3 -u /app/ldoctor.py
