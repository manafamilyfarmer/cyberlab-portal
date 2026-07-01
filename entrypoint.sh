#!/bin/sh
# Load secrets from the read-only bind mount (daemon mounts them; they stay
# root:root 600 on the host and inside the container). We start as root only to
# read them, then drop to the unprivileged "app" user before exec'ing the
# service command. Secret values are never echoed.
set -e

for f in /run/portal-secrets/portaldb.env /run/portal-secrets/portal-app.env; do
    if [ -r "$f" ]; then
        set -a
        . "$f"
        set +a
    fi
done

# Ensure the submissions volume exists, owned by the app user, mode 700
# (files themselves are written 0600 by the upload view). Runs as root before
# dropping privileges so the daemon-mounted volume gets the right ownership.
SUBMISSIONS_DIR="${SUBMISSIONS_DIR:-/var/cyberlab-submissions}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$SUBMISSIONS_DIR"
    chown app:app "$SUBMISSIONS_DIR"
    chmod 700 "$SUBMISSIONS_DIR"
    exec gosu app "$@"
fi
exec "$@"
