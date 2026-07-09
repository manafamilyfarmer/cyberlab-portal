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

# Stage app-readable copies of the WireGuard peer configs + manifest (B4.4).
# The host files stay root:root 600 on the read-only bind mount, which the
# unprivileged "app" user cannot read. While still root we copy them into the
# ephemeral tmpfs (app:app 0400), exactly like the PVE token above. The portal
# reads configs from WG_APP_DIR (settings.WG_SECRETS_DIR) and streams them; the
# bytes never enter the repo, the DB, or any log. Re-staged fresh each boot.
WG_SRC_DIR="${WG_SOURCE_DIR:-/run/portal-secrets/wg}"
WG_APP_DIR="${WG_SECRETS_DIR:-/run/portal-app-secrets/wg}"
if [ "$(id -u)" = "0" ] && [ -d "$WG_SRC_DIR" ]; then
    WG_APP_PARENT=$(dirname "$WG_APP_DIR")
    mkdir -p "$WG_APP_DIR"
    chown root:app "$WG_APP_PARENT" "$WG_APP_DIR"
    chmod 750 "$WG_APP_PARENT" "$WG_APP_DIR"
    for f in "$WG_SRC_DIR"/manifest.tsv "$WG_SRC_DIR"/*.conf; do
        [ -r "$f" ] || continue
        base=$(basename "$f")
        cp "$f" "$WG_APP_DIR/$base"
        chown app:app "$WG_APP_DIR/$base"
        chmod 400 "$WG_APP_DIR/$base"
    done
fi

# Stage an app-readable copy of the read-only WireGuard STATUS SSH key (B4.5).
# Same pattern as the wg configs above: the host file stays root:root 600 on the
# read-only bind mount (app cannot read it), so while still root we copy it into
# ephemeral tmpfs as app:app 0400. The poller reads it via settings.WG_STATUS_KEY.
# Only the PRIVATE key is staged; the pinned known_hosts (public, non-secret) is
# baked into the image under deploy/wg-status/.
WGS_SRC_KEY="${WG_STATUS_SOURCE_KEY:-/run/portal-secrets/wg-status/id_wgstatus}"
WGS_APP_KEY="${WG_STATUS_KEY:-/run/portal-app-secrets/wg-status/id_wgstatus}"
if [ "$(id -u)" = "0" ] && [ -r "$WGS_SRC_KEY" ]; then
    WGS_APP_DIR=$(dirname "$WGS_APP_KEY")
    WGS_APP_PARENT=$(dirname "$WGS_APP_DIR")
    mkdir -p "$WGS_APP_DIR"
    chown root:app "$WGS_APP_PARENT" "$WGS_APP_DIR"
    chmod 750 "$WGS_APP_PARENT" "$WGS_APP_DIR"
    cp "$WGS_SRC_KEY" "$WGS_APP_KEY"
    chown app:app "$WGS_APP_KEY"
    chmod 400 "$WGS_APP_KEY"
fi

# Ensure the submissions volume exists, owned by the app user, mode 700
# (files themselves are written 0600 by the upload view). Runs as root before
# dropping privileges so the daemon-mounted volume gets the right ownership.
SUBMISSIONS_DIR="${SUBMISSIONS_DIR:-/var/cyberlab-submissions}"

# Ensure the audit JSON-stream dir/file exist for the structured SIEM sink
# (Wazuh part 1). Dir app-user-owned 0750; the JSONL file 0640 (NOT world-
# readable). This file is the SIEM ingestion source and is never web-served.
AUDIT_LOG_DIR="${AUDIT_LOG_DIR:-/var/cyberlab-portal-logs}"
AUDIT_LOG_PATH="${AUDIT_LOG_PATH:-$AUDIT_LOG_DIR/audit.jsonl}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$SUBMISSIONS_DIR"
    chown app:app "$SUBMISSIONS_DIR"
    chmod 700 "$SUBMISSIONS_DIR"

    mkdir -p "$AUDIT_LOG_DIR"
    chown app:app "$AUDIT_LOG_DIR"
    chmod 750 "$AUDIT_LOG_DIR"
    if [ ! -e "$AUDIT_LOG_PATH" ]; then
        : > "$AUDIT_LOG_PATH"
    fi
    chown app:app "$AUDIT_LOG_PATH"
    chmod 640 "$AUDIT_LOG_PATH"

    # 0027 so any file the app creates later (e.g. a rotated audit.jsonl.1) is at
    # most 0640 — never world-readable. The submissions view still chmods 0600.
    umask 0027
    exec gosu app "$@"
fi
umask 0027
exec "$@"
