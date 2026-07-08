"""B2 orphan-reaper proof driver.

Phases:
  --phase dryclean : reap_orphans(dry_run=True) on a clean lab -> nothing to reap.
  --phase setup    : build a REAL orphan (clone 153->9000 'b2-orphan-9000', start,
                     then delete its DB reservation -> VM with no reservation + an
                     orphaned lease); a LEGITIMATE running instance (9002 with an
                     intact reservation, must be SKIPPED); and a STALE reservation
                     (VMInstance 9001 reserved, no Proxmox VM). Also unit-proves the
                     name-prefix skip on a synthetic non-portal VM.
  --phase reap     : reap_orphans(grace=1) -> orphan destroyed + lease released,
                     stale cleaned, legit skipped. Independent mgmt01 checks.
  --phase idem     : reap_orphans(grace=1) again -> no-op.
  --phase teardown : destroy the legit VM + delete all throwaway -> zero residue.
"""
import json
import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.curriculum.models import Course, LabExercise, Module
from apps.labs.models import IPLease, LabInstance, Role, VMInstance
from apps.provisioning.pve import GuardError, ProxmoxClient
from apps.provisioning.reaper import reap_orphans, vm_reap_decision

PFX = "b2rp"
ORPHAN_VMID = 9000
STALE_VMID = 9001
LEGIT_VMID = 9002


def _lease_one(vm=None):
    lease = (IPLease.objects.select_for_update()
             .filter(state=IPLease.State.FREE).order_by("ip").first())
    lease.state = IPLease.State.LEASED
    lease.leased_at = timezone.now()
    lease.vm_instance = vm
    lease.save(update_fields=["state", "leased_at", "vm_instance"])
    return lease


class Command(BaseCommand):
    help = "B2 orphan-reaper proof driver."

    def add_arguments(self, parser):
        parser.add_argument(
            "--phase",
            choices=["dryclean", "setup", "reap", "idem", "teardown"],
            required=True)

    def handle(self, *args, **opts):
        out = getattr(self, f"_{opts['phase']}")()
        self.stdout.write("=== B2RP_JSON_BEGIN ===")
        self.stdout.write(json.dumps(out, indent=2, default=str))
        self.stdout.write("=== B2RP_JSON_END ===")

    def _scaffold(self):
        c, _ = Course.objects.get_or_create(slug=f"{PFX}-course",
                                            defaults={"name": "B2RP Course"})
        m, _ = Module.objects.get_or_create(course=c, code="B2RP",
                                            defaults={"title": "B2RP Module"})
        e, _ = LabExercise.objects.get_or_create(module=m, slug=f"{PFX}-e1",
                                                 defaults={"title": "B2RP E1"})
        return e

    def _new_lab(self, exercise):
        return LabInstance.objects.create(
            lab_exercise=exercise, status=LabInstance.Status.RUNNING,
            provisioning_mode=LabInstance.ProvisioningMode.SHARED)

    # ------------------------------------------------------ dryclean ----------
    def _dryclean(self):
        res = reap_orphans.apply(kwargs={"dry_run": True, "grace": 1}).get()
        return {"phase": "dryclean", "summary": res,
                "pass": (not res.get("vm_reaped") and not res.get("reservation_cleaned")
                         and not res.get("lease_released"))}

    # ------------------------------------------------------ setup -------------
    def _setup(self):
        e = self._scaffold()
        client = ProxmoxClient()
        out = {"phase": "setup"}

        # --- ORPHAN: clone+start 9000, record reservation, then delete it -----
        up = client.clone(153, ORPHAN_VMID, "b2-orphan-9000", full=True, pool=client.pool)
        client.wait_task(up, timeout=600)
        client.start(ORPHAN_VMID)
        client.wait_status(ORPHAN_VMID, "running", timeout=120)
        with transaction.atomic():
            lab_o = self._new_lab(e)
            vm_o = VMInstance.objects.create(
                lab_instance=lab_o, vmid=ORPHAN_VMID, role=Role.TARGET,
                proxmox_status="running", source_template_vmid=153,
                hostname="b2-orphan-9000")
            lease_o = _lease_one(vm_o)
        orphan_ip = str(lease_o.ip)
        # simulate crash-after-clone: drop the reservation row, LEAVE the lease
        # leased-but-unbound (SET_NULL) and the VM running -> a true orphan.
        lab_o_id = lab_o.pk
        vm_o.delete()
        lab_o.delete()
        out["orphan"] = {"vmid": ORPHAN_VMID, "ip": orphan_ip,
                         "reservation_deleted": True,
                         "lease_now_unbound": IPLease.objects.get(pk=lease_o.pk).vm_instance_id is None}

        # --- LEGIT: clone+start 9002 with an INTACT reservation (keep) --------
        up = client.clone(153, LEGIT_VMID, "b2-legit-9002", full=True, pool=client.pool)
        client.wait_task(up, timeout=600)
        client.start(LEGIT_VMID)
        client.wait_status(LEGIT_VMID, "running", timeout=120)
        with transaction.atomic():
            lab_l = self._new_lab(e)
            vm_l = VMInstance.objects.create(
                lab_instance=lab_l, vmid=LEGIT_VMID, role=Role.TARGET,
                proxmox_status="running", source_template_vmid=153,
                hostname="b2-legit-9002", ip_applied=True)
            lease_l = _lease_one(vm_l)
        out["legit"] = {"vmid": LEGIT_VMID, "ip": str(lease_l.ip),
                        "lab_id": lab_l.pk, "reservation": True}

        # --- STALE RESERVATION: reserved 9001 with NO Proxmox VM --------------
        with transaction.atomic():
            lab_s = self._new_lab(e)
            lab_s.status = LabInstance.Status.PENDING
            lab_s.save(update_fields=["status"])
            vm_s = VMInstance.objects.create(
                lab_instance=lab_s, vmid=STALE_VMID, role=Role.TARGET,
                proxmox_status="reserved", source_template_vmid=153,
                hostname="b2-batch-stale-9001")
            lease_s = _lease_one(vm_s)
        out["stale_reservation"] = {"vmid": STALE_VMID, "ip": str(lease_s.ip),
                                    "lab_id": lab_s.pk,
                                    "no_proxmox_vm": not client.get_status(STALE_VMID).get("exists")}

        # --- unit-prove reaper v2 keys on RESERVATION, not name ---------------
        # A 9000-range VM with NO reservation, aged past grace, IS reaped even
        # though its name has no "b2-" prefix (DEFECT 1 fix). The same VM WITH an
        # active reservation is SKIPPED — reservation is the real protector.
        reap_norsv, reasons_norsv = vm_reap_decision(
            {"vmid": 9050, "name": "s07-kali-9050"}, set(), grace=1, age=99999)
        reap_rsv, reasons_rsv = vm_reap_decision(
            {"vmid": 9050, "name": "s07-kali-9050"}, {9050}, grace=1, age=99999)
        out["reap_by_reservation"] = {
            "noreservation_reaped": reap_norsv,
            "reservation_skipped": reap_rsv,
            "skip_reasons": reasons_rsv,
            "pass": (reap_norsv is True and reap_rsv is False
                     and any("reservation" in r for r in reasons_rsv))}
        out["ip_pool_free"] = IPLease.objects.filter(state="free").count()
        return out

    # ------------------------------------------------------ reap --------------
    def _reap(self):
        client = ProxmoxClient()
        # guard proof: reaper never acts outside 9000-9099 / on never-touch
        guard = {}
        for label, vmid in (("106", 106), ("109", 109), ("110", 110)):
            try:
                client.destroy(vmid)
                guard[label] = "FAIL: no raise"
            except GuardError:
                guard[label] = "PASS: GuardError"

        max_before = AuditLog.objects.order_by("-id").values_list("id", flat=True).first() or 0
        res = reap_orphans.apply(kwargs={"dry_run": False, "grace": 1}).get()

        # independent mgmt01-token checks are done in bash; here report DB + client view
        orphan_gone = not client.get_status(ORPHAN_VMID).get("exists")
        legit_alive = client.get_status(LEGIT_VMID).get("exists")
        legit_reserved = VMInstance.objects.filter(vmid=LEGIT_VMID).exists()
        stale_cleaned = not VMInstance.objects.filter(vmid=STALE_VMID).exists()
        audits = list(AuditLog.objects.filter(id__gt=max_before, action__startswith="reaper.")
                      .values_list("action", "target_id", "detail"))
        return {
            "phase": "reap", "guard_never_touch": guard, "summary": res,
            "orphan_9000_gone": orphan_gone,
            "legit_9002_alive": legit_alive, "legit_9002_reservation_intact": legit_reserved,
            "stale_9001_cleaned": stale_cleaned,
            "reaper_audits": audits,
            "ip_pool_free": IPLease.objects.filter(state="free").count(),
            "pass": (orphan_gone and legit_alive and legit_reserved and stale_cleaned
                     and all(v.startswith("PASS") for v in guard.values())
                     and len(res.get("vm_reaped", [])) == 1
                     and len(res.get("reservation_cleaned", [])) == 1),
        }

    # ------------------------------------------------------ idem --------------
    def _idem(self):
        res = reap_orphans.apply(kwargs={"dry_run": False, "grace": 1}).get()
        return {"phase": "idem", "summary": res,
                "pass": (not res.get("vm_reaped") and not res.get("reservation_cleaned")
                         and not res.get("lease_released"))}

    # ------------------------------------------------------ teardown ----------
    def _teardown(self):
        client = ProxmoxClient()
        out = {"phase": "teardown"}
        # destroy the legit VM + release its lease + delete rows
        st = client.get_status(LEGIT_VMID)
        if st.get("exists"):
            if (st.get("data") or {}).get("status") == "running":
                client.stop(LEGIT_VMID)
                client.wait_status(LEGIT_VMID, "stopped", timeout=30)
            du = client.destroy(LEGIT_VMID, purge=True)
            client.wait_task(du, timeout=300)
        out["legit_gone"] = not client.get_status(LEGIT_VMID).get("exists")
        try:
            with transaction.atomic():
                for lease in IPLease.objects.filter(state=IPLease.State.LEASED):
                    lease.state = IPLease.State.FREE
                    lease.vm_instance = None
                    lease.released_at = timezone.now()
                    lease.leased_at = None
                    lease.save(update_fields=["state", "vm_instance", "released_at", "leased_at"])
                VMInstance.objects.all().delete()
                LabInstance.objects.all().delete()
                Course.objects.filter(slug=f"{PFX}-course").delete()
            out["cleanup"] = "ok"
        except Exception as exc:  # noqa: BLE001
            out["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        free = IPLease.objects.filter(state="free").count()
        total = IPLease.objects.count()
        out["ip_pool"] = {"free": free, "total": total, "full": free == total}
        out["labinstances"] = LabInstance.objects.count()
        out["vminstances"] = VMInstance.objects.count()
        out["pass"] = (out["legit_gone"] and out["ip_pool"]["full"]
                       and out["labinstances"] == 0 and out["vminstances"] == 0)
        return out
