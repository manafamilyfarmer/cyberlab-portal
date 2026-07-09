# wg-status pinned known_hosts (B4.5)

`known_hosts` pins vpn01's (192.168.100.7) SSH host public key for the read-only
WireGuard status channel. The host **public** key is NOT a secret — it is
committed on purpose so the shipped image verifies the server with
`StrictHostKeyChecking=yes` (never `accept-new`).

Pin source: vpn01's presented ed25519 host key, captured via `ssh-keyscan` and
cross-checked from two independent hosts (mgmt01 and VM114) over the trusted
management LAN — identical both times (no MITM). If vpn01's host key is ever
rotated, update this file with the new key and rebuild the image.

Baked into the image via `COPY . /app`; referenced by `settings.WG_STATUS_KNOWN_HOSTS`.
The SSH **private** key (`id_wgstatus`) is a secret and is NEVER committed — it is
bind-mounted read-only and staged app-readable by `entrypoint.sh`.
