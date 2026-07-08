"""Orphan reaper — periodic guarded sweep (Celery beat).

Defense-in-depth for the reserve-then-clone provisioner. Abnormal cases (worker
crash mid-clone, a Proxmox clone that succeeded while the DB write failed, a
manually-left probe VM) can leave a 9000-range VM with NO active reservation, or
a stale DB reservation with no VM. This task closes both — conservatively.

A VM is destroyed ONLY if ALL of these hold (AND) -- reaper v2:
  (a) VMID in 9000-9099            -- _guard() gates every destroy
  (b) NO active DB VMInstance references it (an active RESERVATION is the real
      protector: reserve-then-clone creates it BEFORE the clone, so every
      in-flight/legit/persistent box -- including the 10 pilot boxes -- has one)
  (c) it is older than REAPER_GRACE

v2 changed two things from v1:
  * DEFECT 1 (reap by reservation, not name): v1 ALSO required the name to start
    with REAPER_NAME_PREFIX ("b2-"). Per-student clones are named s<NN>-kali-<vmid>,
    so a FAILED student clone that left an orphan did NOT match the prefix and was
    un-reapable -- it then blocked the next reservation of that vmid with "config
    file already exists" (the 9006 wedge). The name gate is REMOVED; the name is
    kept only as an audit field. Reservation state alone protects real boxes.
  * DEFECT 2 (unknown age can't linger forever): age = uptime if running, else the
    qmclone task starttime, else the config ctime. If age is STILL undeterminable,
    v1 skipped the orphan forever. v2 instead records a first-seen stamp
    (ReaperSighting) on the first sweep and reaps on a later sweep once
    (now - first_seen) >= grace -- so an un-ageable orphan is reaped within ~2
    grace windows at worst. A box with an active reservation is never stamped.

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
from apps.labs.models import IPLease, LabInstance, ReaperSighting, VMInstance

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


def vm_reap_decision(vm, reserved_vmids, grace, age):
    """Pure decision: return (reap: bool, reasons: list). reap=True ONLY when ALL
    hold: (a) vmid in 9000-9099, (b) NO active DB reservation, (c) age >= grace.
    Reaper v2 keys on RESERVATION STATE, not the VM name (see module docstring) —
    the name is audit-only. Unit-provable in isolation. `age` is the EFFECTIVE age
    the caller resolved (real age, or first-seen-derived age for an un-ageable
    orphan); age=None here means genuinely undeterminable -> do not reap on this
    pass."""
    vmid = vm.get("vmid")
    reasons = []
    if not (TARGET_VMID_MIN <= int(vmid) <= TARGET_VMID_MAX):
        reasons.append("vmid out of 9000-9099")            # (a) — never listed anyway
    if int(vmid) in reserved_vmids:
        reasons.append("has active DB reservation")         # (b)
    if age is None:
        reasons.append("age undeterminable")                # (c)
    elif age < grace:
        reasons.append(f"age {age}s < grace {grace}s")      # (c)
    return (not reasons), reasons


def _vm_age_seconds(client, vm):
    """Resolve a 9000-range VM's age (seconds): uptime if running, else the
    qmclone task starttime, else the config ctime. None if all three fail — the
    caller then falls back to the first-seen stamp so it can't linger forever."""
    if vm.get("status") == "running" and vm.get("uptime"):
        return int(vm["uptime"])
    start = client.clone_task_starttime(vm["vmid"])
    if start:
        return max(0, int(time.time()) - int(start))
    ctime = client.vm_config_ctime(vm["vmid"])
    if ctime:
        return max(0, int(time.time()) - int(ctime))
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

    # --- 1) ORPHAN VMs: reap by RESERVATION STATE (v2), aged past grace --------
    for vm in live_vms:
        vmid, name = vm["vmid"], (vm.get("name") or "")
        in_range = TARGET_VMID_MIN <= int(vmid) <= TARGET_VMID_MAX
        reserved = int(vmid) in reserved_vmids

        # Protected: a box with an active reservation (EVERY pilot box) or an
        # out-of-range vmid is never a candidate. Clear any stale first-seen stamp
        # it may carry and skip — no age lookup needed for the common case.
        if reserved or not in_range:
            if not dry_run:
                ReaperSighting.objects.filter(vmid=vmid).delete()
            reasons = (["has active DB reservation"] if reserved else []) + \
                      ([] if in_range else ["vmid out of 9000-9099"])
            summary["vm_skipped"].append(
                {"vmid": vmid, "name": name, "reasons": reasons})
            continue

        # Candidate (in range, NO reservation): resolve age.
        age = _vm_age_seconds(client, vm)
        via_first_seen = False
        if age is None:
            # DEFECT 2: an un-ageable orphan must not be skipped forever. Stamp
            # first-seen on this sweep; reap only on a LATER sweep once the stamp
            # itself has aged past grace (never on the first sighting).
            if dry_run:
                summary["vm_skipped"].append(
                    {"vmid": vmid, "name": name, "age": None,
                     "reasons": ["age undeterminable; would stamp first-seen"]})
                continue
            sighting, created = ReaperSighting.objects.get_or_create(
                vmid=vmid, defaults={"name": name})
            stamp_age = (now - sighting.first_seen).total_seconds()
            if created or stamp_age < grace:
                write_audit(None, "reaper.first_seen", target_type="qemu",
                            target_id=vmid, name=name,
                            first_seen_age_s=round(stamp_age),
                            reason="orphan age undeterminable; stamped first-seen, awaiting grace")
                summary["vm_skipped"].append(
                    {"vmid": vmid, "name": name, "age": None,
                     "first_seen_age": round(stamp_age),
                     "reasons": ["age undeterminable; first-seen stamped, awaiting grace window"]})
                continue
            age = round(stamp_age)          # stamp aged past grace -> reap-eligible
            via_first_seen = True

        reap, reasons = vm_reap_decision(vm, reserved_vmids, grace, age)
        if not reap:
            summary["vm_skipped"].append(
                {"vmid": vmid, "name": name, "age": age, "reasons": reasons})
            continue
        entry = {"vmid": vmid, "name": name, "age": age, "via_first_seen": via_first_seen}
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
            ReaperSighting.objects.filter(vmid=vmid).delete()   # self-clean the stamp
            write_audit(None, "reaper.vm_destroyed", target_type="qemu",
                        target_id=vmid, name=name, age_s=age,
                        via_first_seen=via_first_seen,
                        reason="orphan: 9000-range + no-reservation + age>=grace (reaper v2)")
            logger.warning("reaper destroyed orphan VM %s (%s), age=%ss, via_first_seen=%s",
                           vmid, name, age, via_first_seen)
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
