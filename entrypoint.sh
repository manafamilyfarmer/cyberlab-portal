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

# Stage an app-readable copy of the Proxmox provisioning token WITHOUT
# shell-sourcing it (the value is untrusted for the shell): the host file stays
# root:root 600 on the read-only mount, so while we are still root we copy it
# into ephemeral /run as app:app 0400. apps.provisioning.pve then PARSES this
# copy. The copy lives only in the container's tmpfs and is re-staged each boot.
PVE_SRC=/run/portal-secrets/portal-pve.env
PVE_APP_DIR=/run/portal-app-secrets
if [ "$(id -u)" = "0" ] && [ -r "$PVE_SRC" ]; then
    mkdir -p "$PVE_APP_DIR"
    chown root:app "$PVE_APP_DIR"
    chmod 750 "$PVE_APP_DIR"
    cp "$PVE_SRC" "$PVE_APP_DIR/portal-pve.env"
    chown app:app "$PVE_APP_DIR/portal-pve.env"
    chmod 400 "$PVE_APP_DIR/portal-pve.env"
fi

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
