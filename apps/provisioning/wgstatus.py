"""B4.5 — read-only WireGuard connection-status poll of vpn01.

The portal is READ-ONLY toward vpn01: it opens a locked-down forced-command SSH
channel (`wgstatus@vpn01`) that prints one line per peer:

    <client_pubkey>\t<handshake_epoch>\t<rx>\t<tx>

We map each pubkey to its WireGuardPeer (by client_pubkey), compute a freshness-
based ``connected`` flag, and cache per-peer state in Redis with a short TTL. The
reader side (/api/my-lab) reads the cache; a cache miss => "unknown" (None), and a
dead poller expires to "unknown" rather than serving a stale "connected".

Hard rules honoured:
  * fixed argv (no shell string, no interpolation of untrusted input),
  * StrictHostKeyChecking=yes against a pinned known_hosts (never accept-new),
  * the SSH private key is never logged; SSH failure => "unknown" + a warning,
    never an exception that could take an endpoint down.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone as _dt_tz

from django.conf import settings
from django.core.cache import cache

from apps.labs.models import WireGuardPeer

log = logging.getLogger("cyberlab.wgstatus")

CACHE_PREFIX = "wgstatus:peer:"


def cache_key(peer_id) -> str:
    return f"{CACHE_PREFIX}{peer_id}"


def _now_epoch() -> int:
    return int(datetime.now(_dt_tz.utc).timestamp())


def build_ssh_argv() -> list[str]:
    """Fixed argv for the read-only status channel. No untrusted input is ever
    interpolated — the forced command on vpn01 ignores any argument."""
    return [
        "ssh",
        "-i", str(settings.WG_STATUS_KEY),
        "-T",  # no PTY
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={settings.WG_STATUS_KNOWN_HOSTS}",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(settings.WG_STATUS_SSH_TIMEOUT)}",
        "-o", "PasswordAuthentication=no",
        "-o", "IdentitiesOnly=yes",
        f"{settings.WG_STATUS_USER}@{settings.WG_STATUS_HOST}",
    ]


def fetch_raw() -> str:
    """Run the SSH command and return stdout. Raises on non-zero/timeout — the
    caller (poll_and_cache) turns any failure into 'unknown', never a crash."""
    argv = build_ssh_argv()
    timeout = int(settings.WG_STATUS_SSH_TIMEOUT) + 3
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        # NB: never log stderr verbatim — keep it to a code so no key path/context
        # can leak. (ssh does not print the key, but stay defensive.)
        raise RuntimeError(f"wgstatus ssh returncode={proc.returncode}")
    return proc.stdout


def parse_dump(text: str) -> dict[str, tuple[int, int, int]]:
    """Parse the 4-column dump -> {pubkey: (handshake_epoch, rx, tx)}.

    Tolerant: skips blank/short lines and non-integer numeric fields. A 0/empty
    handshake is kept as 0 (= never connected)."""
    out: dict[str, tuple[int, int, int]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            parts = line.split()
        if len(parts) < 4:
            continue
        pubkey = parts[0]
        try:
            hs = int(parts[1] or 0)
            rx = int(parts[2] or 0)
            tx = int(parts[3] or 0)
        except (ValueError, TypeError):
            continue
        out[pubkey] = (hs, rx, tx)
    return out


def compute_connected(handshake_epoch, now_epoch=None, freshness=None) -> bool:
    if not handshake_epoch or int(handshake_epoch) <= 0:
        return False
    if now_epoch is None:
        now_epoch = _now_epoch()
    if freshness is None:
        freshness = int(settings.WG_STATUS_FRESHNESS_SECONDS)
    return (now_epoch - int(handshake_epoch)) <= int(freshness)


def _iso(epoch) -> str | None:
    if not epoch or int(epoch) <= 0:
        return None
    return datetime.fromtimestamp(int(epoch), _dt_tz.utc).isoformat()


def poll_and_cache() -> dict:
    """Poll vpn01 and refresh the per-peer cache. Never raises.

    Returns a small summary dict for logging/verification. On SSH failure it
    writes NOTHING (existing cache entries simply age out to 'unknown')."""
    if not getattr(settings, "WG_STATUS_ENABLED", True):
        return {"ok": False, "reason": "disabled", "updated": 0}
    try:
        raw = fetch_raw()
    except Exception as exc:  # noqa: BLE001 — monitoring must not crash the app
        log.warning("wgstatus poll failed: %s", type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__, "updated": 0}

    dump = parse_dump(raw)
    peers = {
        p.client_pubkey: p
        for p in WireGuardPeer.objects.filter(active=True).only("id", "client_pubkey")
    }
    now_epoch = _now_epoch()
    ttl = int(settings.WG_STATUS_CACHE_TTL)
    updated = 0
    unknown_pubkeys = 0
    connected_count = 0

    for pubkey, (hs, rx, tx) in dump.items():
        peer = peers.get(pubkey)
        if peer is None:
            unknown_pubkeys += 1
            continue
        connected = compute_connected(hs, now_epoch)
        if connected:
            connected_count += 1
        cache.set(
            cache_key(peer.id),
            {
                "connected": connected,
                "last_handshake": _iso(hs),
                "rx": rx,
                "tx": tx,
                "polled_at": _iso(now_epoch),
            },
            ttl,
        )
        updated += 1

    if unknown_pubkeys:
        log.warning("wgstatus: ignored %d unknown pubkey(s)", unknown_pubkeys)

    return {
        "ok": True,
        "updated": updated,
        "connected": connected_count,
        "unknown_pubkeys": unknown_pubkeys,
        "known_peers": len(peers),
    }


def get_status(peer_id) -> dict:
    """Reader side. Cache miss => unknown (connected=None, last_handshake=None)."""
    v = cache.get(cache_key(peer_id))
    if not v:
        return {"connected": None, "last_handshake": None}
    return {
        "connected": v.get("connected"),
        "last_handshake": v.get("last_handshake"),
    }
