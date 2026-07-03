"""Orphan reaper — periodic guarded sweep (Celery beat).

Defense-in-depth for the reserve-then-clone provisioner. Abnormal cases (worker
crash mid-clone, a Proxmox clone that succeeded while the DB write failed, a
manually-left probe VM) can leave a 9000-range VM with NO active reservation, or
a stale DB reservation with no VM. This task closes both — conservatively.

A VM is destroyed ONLY if ALL FOUR hold (AND):
  (a) VMID in 9000-9099            -- _guard() gates every destroy
  (b) name starts with the portal provisioning prefix (REAPER_NAME_PREFIX)
  (c) NO active DB VMInstance references it
  (d) it is older than REAPER_GRACE  (age = uptime if running, else clone-task
      starttime; unknown age -> SKIP)
Because reserve-then-clone creates the VMInstance reservation BEFORE the clone,
any in-flight/legit VM has a reservation, so (c) already protects it; grace is a
secondary guard.

Stale reservations (VMInstance with no live VM, lab older than grace) -> release
lease + mark LabInstance error (DB-only; nothing to destroy). Orphaned leases
(leased, unbound, older than grace) -> release. Everything is audited; dry_run
logs what WOULD happen without acting. Idempotent.
"""
from __future__ import annotations

import logging
import time

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.audit.services import write_audit
from apps.labs.models import IPLease, LabInstance, VMInstance

from .allocation import release_lease, release_reservation
from .pve import ProxmoxClient, TARGET_VMID_MAX, TARGET_VMID_MIN

logger = logging.getLogger("apps.provisioning.reaper")

REAP_FORCE_STOP = 30   # force stop -> stopped before destroy
REAP_TASK = 300        # destroy task poll cap


def _cfg(name, default):
    return getattr(settings, name, default)


def reaper_grace():
    return int(_cfg("REAPER_GRACE", 900))


def reaper_prefix():
    return str(_cfg("REAPER_NAME_PREFIX", "b2-"))


def vm_reap_decision(vm, reserved_vmids, grace, prefix, age):
    """Pure decision: return (reap: bool, reasons: list). reap=True ONLY when all
    four AND-conditions hold. Used by the sweep and unit-provable in isolation."""
    vmid = vm.get("vmid")
    name = vm.get("name") or ""
    reasons = []
    if not (TARGET_VMID_MIN <= int(vmid) <= TARGET_VMID_MAX):
        reasons.append("vmid out of 9000-9099")           # (a) — never listed anyway
    if not name.startswith(prefix):
        reasons.append(f"name {name!r} lacks prefix {prefix!r}")  # (b)
    if int(vmid) in reserved_vmids:
        reasons.append("has active DB reservation")        # (c)
    if age is None:
        reasons.append("age unknown")                      # (d)
    elif age < grace:
        reasons.append(f"age {age}s < grace {grace}s")     # (d)
    return (not reasons), reasons


def _vm_age_seconds(client, vm):
    if vm.get("status") == "running" and vm.get("uptime"):
        return int(vm["uptime"])
    start = client.clone_task_starttime(vm["vmid"])
    if start:
        return max(0, int(time.time()) - int(start))
    return None


@shared_task(bind=True)
def reap_orphans(self, dry_run=False, grace=None, name_prefix=None):
    if not _cfg("REAPER_ENABLED", True):
        return {"disabled": True}

    grace = int(grace) if grace is not None else reaper_grace()
    prefix = name_prefix if name_prefix is not None else reaper_prefix()
    client = ProxmoxClient()
    now = timezone.now()
    summary = {
        "dry_run": bool(dry_run), "grace": grace, "prefix": prefix,
        "tls_verify": client.verify,
        "vm_reaped": [], "vm_skipped": [],
        "reservation_cleaned": [], "lease_released": [],
    }

    live_vms = client.list_target_vms()               # 9000-range only (guarded list)
    live_vmids = {v["vmid"] for v in live_vms}
    reserved_vmids = set(
        VMInstance.objects.filter(
            vmid__gte=TARGET_VMID_MIN, vmid__lte=TARGET_VMID_MAX
        ).values_list("vmid", flat=True)
    )

    # --- 1) ORPHAN VMs: all four AND-conditions --------------------------------
    for vm in live_vms:
        vmid, name = vm["vmid"], (vm.get("name") or "")
        age = _vm_age_seconds(client, vm)
        reap, reasons = vm_reap_decision(vm, reserved_vmids, grace, prefix, age)
        if not reap:
            summary["vm_skipped"].append(
                {"vmid": vmid, "name": name, "age": age, "reasons": reasons})
            continue
        entry = {"vmid": vmid, "name": name, "age": age}
        if dry_run:
            entry["would_reap"] = True
        else:
            st = client.get_status(vmid)               # guarded
            if st.get("exists") and (st.get("data") or {}).get("status") == "running":
                client.stop(vmid)                      # guarded force stop
                client.wait_status(vmid, "stopped", timeout=REAP_FORCE_STOP)
            du = client.destroy(vmid, purge=True)      # guarded
            client.wait_task(du, timeout=REAP_TASK)
            entry["destroyed"] = not client.get_status(vmid).get("exists")
            # release any lease still bound to this vmid's (now-gone) reservation
            for lp in list(IPLease.objects.filter(vm_instance__vmid=vmid)
                           .values_list("pk", flat=True)):
                release_lease(lp)
            write_audit(None, "reaper.vm_destroyed", target_type="qemu",
                        target_id=vmid, name=name, age_s=age,
                        reason="orphan: 9000-range + prefix + no-reservation + age>grace")
            logger.warning("reaper destroyed orphan VM %s (%s), age=%ss", vmid, name, age)
        summary["vm_reaped"].append(entry)

    # --- 2) STALE RESERVATIONS: reserved vmid with no live VM, aged ------------
    stale = (VMInstance.objects
             .filter(vmid__gte=TARGET_VMID_MIN, vmid__lte=TARGET_VMID_MAX)
             .exclude(vmid__in=live_vmids)
             .select_related("lab_instance"))
    for vm in stale:
        lab = vm.lab_instance
        age = ((now - lab.created_at).total_seconds()
               if lab and lab.created_at else None)
        if age is None or age < grace:
            summary["vm_skipped"].append(
                {"reservation_vmid": vm.vmid, "lab_id": getattr(lab, "pk", None),
                 "age": None if age is None else round(age),
                 "reasons": ["reservation age unknown or < grace"]})
            continue
        entry = {"vmid": vm.vmid, "lab_id": getattr(lab, "pk", None), "age": round(age)}
        if dry_run:
            entry["would_clean"] = True
        else:
            release_reservation(vm)                    # frees lease + deletes row
            if lab is not None:
                lab.status = LabInstance.Status.ERROR
                lab.save(update_fields=["status"])
            write_audit(None, "reaper.reservation_cleaned", target_type="qemu",
                        target_id=vm.vmid, lab_instance_id=getattr(lab, "pk", None),
                        age_s=round(age),
                        reason="stale reservation: no live VM, age>grace")
            logger.warning("reaper cleaned stale reservation vmid=%s lab=%s",
                           vm.vmid, getattr(lab, "pk", None))
        summary["reservation_cleaned"].append(entry)

    # --- 3) ORPHANED LEASES: leased, unbound, aged (belt & suspenders) ---------
    for lease in IPLease.objects.filter(state=IPLease.State.LEASED, vm_instance__isnull=True):
        age = ((now - lease.leased_at).total_seconds()
               if lease.leased_at else None)
        if age is None or age < grace:
            continue
        entry = {"ip": str(lease.ip), "age": round(age)}
        if not dry_run:
            release_lease(lease.pk)
            write_audit(None, "reaper.lease_released", target_type="ip",
                        target_id=str(lease.ip), age_s=round(age),
                        reason="orphaned lease: no vm, age>grace")
        else:
            entry["would_release"] = True
        summary["lease_released"].append(entry)

    return summary
