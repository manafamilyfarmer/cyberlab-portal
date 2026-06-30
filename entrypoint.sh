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

if [ "$(id -u)" = "0" ]; then
    exec gosu app "$@"
fi
exec "$@"
