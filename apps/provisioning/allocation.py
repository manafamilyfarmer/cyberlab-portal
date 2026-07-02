"""VMID + IP allocation and the capacity guard for shared-model provisioning.

Split by trust boundary so the WEB request never has to touch Proxmox:

  * capacity_precheck_db()  -- DB-only. Safe to call from a web request (validate
                               + enqueue). Checks the concurrency cap, the free-IP
                               pool, and DB-recorded 9000-range usage.
  * capacity_ok(client)     -- the authoritative check, run in the WORKER. Adds a
                               live Proxmox enumeration of the 9000-range.
  * allocate_vmid(client)   -- lowest free VMID in 9000..9099 (live Proxmox OR a
                               DB VMInstance record counts as taken).
  * lease_ip()              -- atomically flip one free IPLease free->leased.

allocate_vmid / capacity_ok issue a Proxmox read, so they are WORKER-only.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.labs.models import IPLease, LabInstance, VMInstance

from .pve import ProxmoxClient, TARGET_VMID_MAX, TARGET_VMID_MIN

DEFAULT_MAX_CONCURRENT = 10
_VMID_SLOTS = TARGET_VMID_MAX - TARGET_VMID_MIN + 1


class CapacityError(RuntimeError):
    """No capacity: no free VMID, no free IP, or the concurrency cap is reached."""


def _max_concurrent() -> int:
    return int(getattr(settings, "PROVISION_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT))


def _db_target_vmids() -> set[int]:
    """9000-range VMIDs recorded on live (not-yet-destroyed) VMInstance rows."""
    return set(
        VMInstance.objects.filter(
            vmid__gte=TARGET_VMID_MIN, vmid__lte=TARGET_VMID_MAX
        ).values_list("vmid", flat=True)
    )


def _live_target_vmids(client: ProxmoxClient) -> set[int]:
    return set(client.list_target_vmids())


def active_instance_count(exclude_labinstance_id=None) -> int:
    """Instances that hold (or are about to hold) resources."""
    qs = LabInstance.objects.filter(
        status__in=(LabInstance.Status.PENDING, LabInstance.Status.RUNNING)
    )
    if exclude_labinstance_id is not None:
        qs = qs.exclude(pk=exclude_labinstance_id)
    return qs.count()


def capacity_precheck_db(exclude_labinstance_id=None):
    """DB-only capacity pre-check. Returns (ok, reason). WEB-SAFE (no Proxmox)."""
    cap = _max_concurrent()
    active = active_instance_count(exclude_labinstance_id)
    if active >= cap:
        return False, f"concurrent-instance cap reached ({active}/{cap})"
    if not IPLease.objects.filter(state=IPLease.State.FREE).exists():
        return False, "no free IP in the pool"
    if len(_db_target_vmids()) >= _VMID_SLOTS:
        return False, "no free VMID slot (DB)"
    return True, "ok"


def capacity_ok(client=None, exclude_labinstance_id=None):
    """Authoritative capacity check (WORKER): DB pre-check + live Proxmox VMID
    enumeration. Returns (ok, reason)."""
    ok, reason = capacity_precheck_db(exclude_labinstance_id)
    if not ok:
        return ok, reason
    client = client or ProxmoxClient()
    taken = _live_target_vmids(client) | _db_target_vmids()
    if len(taken) >= _VMID_SLOTS:
        return False, "no free VMID in 9000..9099 (live)"
    return True, "ok"


def allocate_vmid(client=None) -> int:
    """Lowest free VMID in 9000..9099. A VMID is taken if it exists in Proxmox
    (any state) OR is recorded on a live VMInstance row. Raises CapacityError."""
    client = client or ProxmoxClient()
    taken = _live_target_vmids(client) | _db_target_vmids()
    for vmid in range(TARGET_VMID_MIN, TARGET_VMID_MAX + 1):
        if vmid not in taken:
            return vmid
    raise CapacityError(
        f"no free VMID in {TARGET_VMID_MIN}..{TARGET_VMID_MAX} ({len(taken)} in use)"
    )


def lease_ip() -> IPLease:
    """Atomically take one free IPLease (free->leased) under a row lock. The
    caller attaches vm_instance once the VMInstance row exists. RECORD only —
    this does NOT apply an ipconfig to any VM (that is B2.3)."""
    with transaction.atomic():
        lease = (
            IPLease.objects.select_for_update()
            .filter(state=IPLease.State.FREE)
            .order_by("ip")
            .first()
        )
        if lease is None:
            raise CapacityError("no free IPLease in the pool")
        lease.state = IPLease.State.LEASED
        lease.leased_at = timezone.now()
        lease.save(update_fields=["state", "leased_at"])
        return lease


def release_lease(lease_pk) -> None:
    """Return an IPLease to the pool (leased->free). Idempotent."""
    with transaction.atomic():
        try:
            lease = IPLease.objects.select_for_update().get(pk=lease_pk)
        except IPLease.DoesNotExist:
            return
        lease.state = IPLease.State.FREE
        lease.vm_instance = None
        lease.released_at = timezone.now()
        lease.leased_at = None
        lease.save(
            update_fields=["state", "vm_instance", "released_at", "leased_at"]
        )
