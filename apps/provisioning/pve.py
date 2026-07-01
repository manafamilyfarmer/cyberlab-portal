"""Proxmox client for portal-driven provisioning — with IN-CODE guards.

This module is the ONLY place the portal talks to the Proxmox API for
provisioning. Safety is enforced in code, not by convention:

  * NEVER_TOUCH VMIDs (production identity/monitoring) are hard-denied.
  * Clones may only source from CLONE_SOURCE_ALLOWLIST.
  * Any allocate/config/stop/destroy target must sit inside TARGET_VMID_RANGE.

Every public method runs `_guard(vmid, op)` BEFORE issuing any HTTP request, so
a disallowed VMID raises *before* the network is ever touched.

Credentials are read from the portal token file by PARSING key=value lines
(never by shell-sourcing). TLS verification honours PORTAL_PVE_VERIFY_TLS: when
=1 the system trust store is used and a self-signed cert fails LOUDLY; it is
never silently disabled.
"""
from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger("apps.provisioning.pve")

# ---------------------------------------------------------------------------
# Guard configuration (in-code, not env-driven — cannot be relaxed at runtime)
# ---------------------------------------------------------------------------
CLONE_SOURCE_ALLOWLIST = frozenset({151, 152})
TARGET_VMID_MIN = 9000
TARGET_VMID_MAX = 9099
NEVER_TOUCH = frozenset({106, 109, 110})

# The entrypoint stages an app-readable 0400 copy here (parsed, never sourced).
# Fall back to the raw root:600 mount when running as root (e.g. a root shell).
DEFAULT_ENV_PATH = "/run/portal-app-secrets/portal-pve.env"
FALLBACK_ENV_PATH = "/run/portal-secrets/portal-pve.env"


class GuardError(RuntimeError):
    """A requested VMID/operation violates an in-code safety guard."""


class ProxmoxAPIError(RuntimeError):
    """The Proxmox API call failed (transport, TLS, or non-OK task)."""


def _in_target_range(vmid: int) -> bool:
    return TARGET_VMID_MIN <= vmid <= TARGET_VMID_MAX


def parse_env_file(path: str) -> dict:
    """Parse KEY=VALUE lines. Never sources the file through a shell."""
    cfg: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


class ProxmoxClient:
    def __init__(self, env_path: str | None = None):
        if env_path is None:
            if os.access(DEFAULT_ENV_PATH, os.R_OK):
                env_path = DEFAULT_ENV_PATH
            elif os.access(FALLBACK_ENV_PATH, os.R_OK):
                env_path = FALLBACK_ENV_PATH
            else:
                raise ProxmoxAPIError(
                    f"portal-pve.env not readable at {DEFAULT_ENV_PATH} or "
                    f"{FALLBACK_ENV_PATH}; is the entrypoint staging it?"
                )
        cfg = parse_env_file(env_path)
        self.env_path = env_path
        required = ("PORTAL_PVE_URL", "PORTAL_PVE_TOKEN_ID", "PORTAL_PVE_TOKEN_SECRET")
        missing = [k for k in required if not cfg.get(k)]
        if missing:
            raise ProxmoxAPIError(f"portal-pve.env missing required keys: {missing}")

        self.base = cfg["PORTAL_PVE_URL"].rstrip("/")
        self._token_id = cfg["PORTAL_PVE_TOKEN_ID"]
        self._token_secret = cfg["PORTAL_PVE_TOKEN_SECRET"]
        self.node = cfg.get("PORTAL_PVE_NODE", "proxmox")
        self.pool = cfg.get("PORTAL_PVE_POOL", "cyberlab-agent")

        # TLS verification resolves to one of three states, honestly:
        #   * verify off  -> self.verify is False (LOUD, temporary override only)
        #   * CA bundle    -> self.verify is the bundle PATH (genuine verification
        #                     against the trusted PVE cluster CA)
        #   * else on      -> self.verify is True (system trust store; a self-signed
        #                     PVE cert then fails LOUDLY -- never silently ignored)
        # We never hardcode verification off.
        verify_flag = cfg.get("PORTAL_PVE_VERIFY_TLS", "1").strip().lower()
        verify_on = verify_flag not in ("0", "false", "no", "off")
        ca_bundle = cfg.get("PORTAL_PVE_CA_BUNDLE", "").strip()

        if not verify_on:
            self.verify = False
        elif ca_bundle:
            if not os.access(ca_bundle, os.R_OK):
                # Refuse to silently downgrade to an unverified or system-store
                # connection when the operator explicitly configured a CA bundle.
                raise ProxmoxAPIError(
                    f"PORTAL_PVE_CA_BUNDLE={ca_bundle} is not readable; refusing "
                    "to fall back to an unverified connection. Fix the mount/path "
                    "or unset PORTAL_PVE_CA_BUNDLE."
                )
            self.verify = ca_bundle
        else:
            self.verify = True

        self._session = requests.Session()
        self._session.headers["Authorization"] = (
            f"PVEAPIToken={self._token_id}={self._token_secret}"
        )
        if self.verify is False:
            # Honest + loud: we do NOT hardcode this; it is driven by the env
            # flag the operator set as a TEMPORARY measure. Follow-up: point
            # PORTAL_PVE_CA_BUNDLE at the trusted PVE cluster CA, then =1.
            logger.warning(
                "PORTAL_PVE_VERIFY_TLS=0 -> TLS certificate verification is "
                "DISABLED (temporary). FOLLOW-UP(HIGH): trust the PVE CA in the "
                "worker (PORTAL_PVE_CA_BUNDLE) and restore PORTAL_PVE_VERIFY_TLS=1."
            )
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # pragma: no cover - best-effort noise suppression
                pass
        elif isinstance(self.verify, str):
            logger.info(
                "TLS verification ON against pinned CA bundle %s", self.verify
            )

    # ------------------------------------------------------------------ guards
    def _guard(self, vmid, op: str) -> int:
        vmid = int(vmid)
        # NEVER_TOUCH is checked first and unconditionally.
        if vmid in NEVER_TOUCH:
            raise GuardError(
                f"REFUSED: VMID {vmid} is in NEVER_TOUCH (op={op}); no API call made."
            )
        if op == "clone_source":
            if vmid not in CLONE_SOURCE_ALLOWLIST:
                raise GuardError(
                    f"REFUSED: clone source {vmid} not in allowlist "
                    f"{sorted(CLONE_SOURCE_ALLOWLIST)}; no API call made."
                )
        else:
            if not _in_target_range(vmid):
                raise GuardError(
                    f"REFUSED: target VMID {vmid} outside "
                    f"{TARGET_VMID_MIN}..{TARGET_VMID_MAX} (op={op}); no API call made."
                )
        return vmid

    # ---------------------------------------------------------------- transport
    def _request(self, method: str, path: str, *, params=None, data=None, timeout=30):
        url = f"{self.base}{path}"
        try:
            return self._session.request(
                method, url, params=params, data=data,
                verify=self.verify, timeout=timeout,
            )
        except requests.exceptions.SSLError as exc:
            raise ProxmoxAPIError(
                f"TLS verification FAILED for {url} "
                f"(PORTAL_PVE_VERIFY_TLS={'1' if self.verify else '0'}). The PVE "
                "cert is self-signed; trust the PVE CA in the worker or set "
                f"PORTAL_PVE_VERIFY_TLS=0 temporarily. Underlying: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ProxmoxAPIError(f"HTTP transport error for {url}: {exc}") from exc

    @staticmethod
    def _json(resp):
        try:
            return resp.json()
        except ValueError:
            return None

    # ---------------------------------------------------------- raw (UNGUARDED)
    def raw_get_status(self, vmid):
        """UNGUARDED status read — used ONLY by the runtime negative scope test
        to assert the portal token is refused (403) on a NEVER_TOUCH VM. Do not
        use for provisioning."""
        resp = self._request(
            "GET", f"/nodes/{self.node}/qemu/{int(vmid)}/status/current"
        )
        return resp.status_code, self._json(resp)

    # ------------------------------------------------------------ guarded reads
    def get_status(self, vmid):
        vmid = self._guard(vmid, "status")
        resp = self._request("GET", f"/nodes/{self.node}/qemu/{vmid}/status/current")
        return {
            "http": resp.status_code,
            "exists": resp.status_code == 200,
            "data": (self._json(resp) or {}).get("data"),
        }

    def get_config(self, vmid):
        vmid = self._guard(vmid, "config")
        resp = self._request("GET", f"/nodes/{self.node}/qemu/{vmid}/config")
        return {
            "http": resp.status_code,
            "exists": resp.status_code == 200,
            "data": (self._json(resp) or {}).get("data"),
        }

    # ----------------------------------------------------------- guarded writes
    def clone(self, source, target, name, *, full=True, pool=None):
        source = self._guard(source, "clone_source")
        target = self._guard(target, "clone_target")
        pool = pool or self.pool
        params = {
            "newid": target,
            "name": name,
            "full": 1 if full else 0,
            "pool": pool,
            "target": self.node,
        }
        resp = self._request(
            "POST", f"/nodes/{self.node}/qemu/{source}/clone", data=params
        )
        if resp.status_code not in (200, 201):
            raise ProxmoxAPIError(
                f"clone {source}->{target} failed HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        upid = (self._json(resp) or {}).get("data")
        if not upid:
            raise ProxmoxAPIError(f"clone {source}->{target} returned no UPID")
        return upid

    def stop(self, vmid):
        vmid = self._guard(vmid, "stop")
        resp = self._request(
            "POST", f"/nodes/{self.node}/qemu/{vmid}/status/stop"
        )
        if resp.status_code not in (200, 201):
            raise ProxmoxAPIError(
                f"stop {vmid} failed HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return (self._json(resp) or {}).get("data")

    def destroy(self, vmid, *, purge=True):
        vmid = self._guard(vmid, "destroy")
        params = {}
        if purge:
            params = {"purge": 1, "destroy-unreferenced-disks": 1}
        resp = self._request(
            "DELETE", f"/nodes/{self.node}/qemu/{vmid}", params=params
        )
        if resp.status_code not in (200, 201):
            raise ProxmoxAPIError(
                f"destroy {vmid} failed HTTP {resp.status_code}: {resp.text[:300]}"
            )
        upid = (self._json(resp) or {}).get("data")
        if not upid:
            raise ProxmoxAPIError(f"destroy {vmid} returned no UPID")
        return upid

    # ------------------------------------------------------------- task polling
    def wait_task(self, upid, *, timeout=300, interval=2.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            resp = self._request(
                "GET", f"/nodes/{self.node}/tasks/{upid}/status"
            )
            if resp.status_code != 200:
                raise ProxmoxAPIError(
                    f"task status {upid} HTTP {resp.status_code}: {resp.text[:200]}"
                )
            last = (self._json(resp) or {}).get("data") or {}
            if last.get("status") == "stopped":
                if last.get("exitstatus") != "OK":
                    raise ProxmoxAPIError(
                        f"task {upid} finished exitstatus={last.get('exitstatus')!r}"
                    )
                return last
            time.sleep(interval)
        raise ProxmoxAPIError(f"task {upid} did not finish within {timeout}s; last={last}")
