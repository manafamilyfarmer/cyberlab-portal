"""B2 Step 4a proof driver — atomic reserve-then-clone allocator under REAL
concurrency.

Phases:
  --phase conctest : FAST unit-style atomicity test. N parallel THREADS (own DB
                     connections) call the reserve path against the DB at once
                     (a barrier maximizes contention). Assert N distinct vmids +
                     N distinct IPs, no error, no duplicate. Then release all N.
                     No clone — directly proves DB arbitration.
  --phase up2      : REAL concurrent double-provision. Instructor I1 owns batches
                     B1 & B2; fire TWO provision tasks back-to-back so the worker
                     (concurrency 2) runs them in PARALLEL. Assert both succeed
                     with DIFFERENT vmids + IPs, no "config file already exists".
  --phase retry    : RETRY-safety. Re-invoke a completed provision's id -> must be
                     an idempotent NO-OP (no second VM, no orphan).
  --phase down     : deprovision both -> destroyed; delete throwaway data.
"""
import json
import threading
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import InstructorProfile, StudentProfile, User
from apps.curriculum.models import Batch, Course, LabExercise, Module
from apps.labs.models import IPLease, LabInstance, VMInstance
from apps.provisioning.allocation import (
    allocate_and_reserve_vmid,
    lease_ip,
    release_lease,
    release_reservation,
)
from apps.provisioning.api import AdminLabInstanceViewSet
from apps.provisioning.tasks import provision_shared_instance

PFX = "b2s4a"
# Two full clones run in PARALLEL (worker concurrency 2) and contend for storage
# I/O; with B2.3 each also waits ~220s for cloud-init/agent to apply the IP. The
# driver polls up to this cap but returns early once both instances settle.
POLL_CAP = 900
POLL_INTERVAL = 3.0
factory = APIRequestFactory()


class _NoLiveVMs:
    """Stub Proxmox client for the DB-only atomicity test: no live 9000-range
    VMs (verified empty in pre-flight), so allocation is arbitrated purely by the
    DB unique constraint — which is exactly what we want to exercise."""

    def list_target_vmids(self):
        return []


def _staff(user):
    user.is_verified = lambda: True
    return user


def _create_view():
    return AdminLabInstanceViewSet.as_view({"post": "create"})


def _deprovision_view():
    return AdminLabInstanceViewSet.as_view({"post": "deprovision"})


def _scaffold():
    course, _ = Course.objects.get_or_create(
        slug=f"{PFX}-course", defaults={"name": "B2S4a Course"})
    module, _ = Module.objects.get_or_create(
        course=course, code="B2S4A", defaults={"title": "B2S4a Module"})
    e1, _ = LabExercise.objects.get_or_create(
        module=module, slug=f"{PFX}-e1", defaults={"title": "B2S4a E1"})
    return course, module, e1


class Command(BaseCommand):
    help = "B2 Step 4a atomic-allocator proof driver."

    def add_arguments(self, parser):
        parser.add_argument(
            "--phase", choices=["conctest", "up2", "retry", "down"], required=True)
        parser.add_argument("--n", type=int, default=10)

    def handle(self, *args, **opts):
        out = getattr(self, f"_{opts['phase']}")(opts)
        self.stdout.write("=== B2S4A_JSON_BEGIN ===")
        self.stdout.write(json.dumps(out, indent=2, default=str))
        self.stdout.write("=== B2S4A_JSON_END ===")

    # -------------------------------------------------- 3.1 conctest ----------
    def _conctest(self, opts):
        n = opts["n"]
        course, module, e1 = _scaffold()
        lab = LabInstance.objects.create(
            lab_exercise=e1, status=LabInstance.Status.PENDING,
            provisioning_mode=LabInstance.ProvisioningMode.SHARED)

        results, errors = [], []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def worker(_i):
            try:
                barrier.wait(timeout=30)  # all threads reserve at the same instant
                vm = allocate_and_reserve_vmid(lab, client=_NoLiveVMs())
                lease = lease_ip()
                with lock:
                    results.append({"vmid": vm.vmid, "ip": str(lease.ip),
                                    "vm_pk": vm.pk, "lease_pk": lease.pk})
            except Exception as exc:  # noqa: BLE001 — record, don't crash the test
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                connection.close()  # each thread owns its DB connection

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        vmids = [r["vmid"] for r in results]
        ips = [r["ip"] for r in results]
        out = {
            "phase": "conctest", "n": n,
            "count": len(results), "errors": errors,
            "distinct_vmids": len(set(vmids)), "distinct_ips": len(set(ips)),
            "vmids": sorted(vmids), "ips": sorted(ips),
            "pass": (len(results) == n and not errors
                     and len(set(vmids)) == n and len(set(ips)) == n),
        }
        # release all reservations + leases, delete throwaway. (In this unit
        # test the lease is claimed independently of the reservation row, so free
        # it explicitly by pk — the product path links lease->vm and frees via
        # release_reservation.)
        for r in results:
            release_lease(r["lease_pk"])
            try:
                release_reservation(VMInstance.objects.get(pk=r["vm_pk"]))
            except VMInstance.DoesNotExist:
                pass
        lab.delete()
        Course.objects.filter(slug=f"{PFX}-course").delete()
        out["cleanup_ip_free"] = IPLease.objects.filter(state="free").count()
        out["cleanup_vminstances"] = VMInstance.objects.count()
        return out

    # -------------------------------------------------- 3.2 up2 ---------------
    def _up2(self, opts):
        course, module, e1 = _scaffold()
        i1u, _ = User.objects.get_or_create(
            username=f"{PFX}-i1",
            defaults={"role": User.Role.INSTRUCTOR, "email": "b2s4a-i1@example.invalid"})
        i1, _ = InstructorProfile.objects.get_or_create(user=i1u)
        b1, _ = Batch.objects.get_or_create(
            course=course, name=f"{PFX}-B1", defaults={"instructor": i1})
        b2, _ = Batch.objects.get_or_create(
            course=course, name=f"{PFX}-B2", defaults={"instructor": i1})
        for b in (b1, b2):
            if b.instructor_id != i1.id:
                b.instructor = i1
                b.save(update_fields=["instructor"])

        # Fire TWO provisions back-to-back -> worker (concurrency 2) runs both
        # in parallel. Web only validates + enqueues.
        lab_ids = []
        for b in (b1, b2):
            req = factory.post("/api/admin/labinstances",
                               {"batch": b.pk, "lab_exercise": e1.pk}, format="json")
            force_authenticate(req, user=_staff(i1u))
            resp = _create_view()(req)
            lab_ids.append({"batch": b.pk, "http": resp.status_code,
                            "lab_id": resp.data.get("id") if resp.status_code == 202 else None})

        ids = [x["lab_id"] for x in lab_ids if x["lab_id"]]
        # poll both to running/error
        deadline = time.monotonic() + POLL_CAP
        while time.monotonic() < deadline:
            labs = list(LabInstance.objects.filter(pk__in=ids))
            if all(l.status in (LabInstance.Status.RUNNING, LabInstance.Status.ERROR)
                   for l in labs):
                break
            time.sleep(POLL_INTERVAL)

        detail = []
        for lid in ids:
            l = LabInstance.objects.get(pk=lid)
            vm = l.vms.first()
            detail.append({
                "lab_id": lid, "batch": l.owner_batch_id, "status": l.status,
                "vmid": vm.vmid if vm else None,
                "ip": str(vm.ip.ip) if vm and vm.ip_id else None,
                "proxmox_status": vm.proxmox_status if vm else None})

        vmids = [d["vmid"] for d in detail if d["vmid"] is not None]
        ips = [d["ip"] for d in detail if d["ip"] is not None]
        both_running = len(detail) == 2 and all(
            d["status"] == LabInstance.Status.RUNNING for d in detail)
        return {
            "phase": "up2", "enqueue": lab_ids, "instances": detail,
            "distinct_vmids": len(set(vmids)) == len(vmids) and len(vmids) == 2,
            "distinct_ips": len(set(ips)) == len(ips) and len(ips) == 2,
            "both_running": both_running,
            "pass": both_running and len(set(vmids)) == 2 and len(set(ips)) == 2,
        }

    # -------------------------------------------------- 3.3 retry -------------
    def _retry(self, opts):
        """Re-invoke a completed provision's id synchronously -> must be an
        idempotent NO-OP (no second VM row, no orphan)."""
        lab = (LabInstance.objects.filter(owner_batch__name=f"{PFX}-B1",
                                          status=LabInstance.Status.RUNNING)
               .order_by("-created_at").first())
        if lab is None:
            return {"phase": "retry", "verdict": "BLOCKED (no running B1 instance)"}
        before_vms = list(lab.vms.values_list("id", "vmid"))
        before_ipfree = IPLease.objects.filter(state="free").count()
        # synchronous re-invoke of the SAME lab id
        res = provision_shared_instance.apply(args=[lab.pk]).get()
        after_vms = list(lab.vms.values_list("id", "vmid"))
        after_ipfree = IPLease.objects.filter(state="free").count()
        return {
            "phase": "retry", "lab_id": lab.pk,
            "reinvoke_verdict": res.get("verdict"),
            "idempotent_noop": res.get("idempotent_noop", False),
            "vms_before": before_vms, "vms_after": after_vms,
            "no_new_vm": before_vms == after_vms,
            "ip_free_unchanged": before_ipfree == after_ipfree,
            "pass": (res.get("idempotent_noop") is True
                     and before_vms == after_vms
                     and before_ipfree == after_ipfree),
        }

    # -------------------------------------------------- 3.4 down --------------
    def _down(self, opts):
        i1u = User.objects.filter(username=f"{PFX}-i1").first()
        labs = list(LabInstance.objects.filter(
            owner_batch__name__in=[f"{PFX}-B1", f"{PFX}-B2"]))
        deprov = []
        for l in labs:
            req = factory.post(f"/api/admin/labinstances/{l.pk}/deprovision")
            force_authenticate(req, user=_staff(i1u))
            resp = _deprovision_view()(req, pk=l.pk)
            deprov.append({"lab_id": l.pk, "http": resp.status_code})

        ids = [l.pk for l in labs]
        deadline = time.monotonic() + POLL_CAP
        while time.monotonic() < deadline:
            ls = list(LabInstance.objects.filter(pk__in=ids))
            if all(l.status in (LabInstance.Status.DESTROYED, LabInstance.Status.ERROR)
                   for l in ls):
                break
            time.sleep(POLL_INTERVAL)
        statuses = {l.pk: l.status for l in LabInstance.objects.filter(pk__in=ids)}

        # cleanup throwaway
        cleanup = {}
        try:
            with transaction.atomic():
                LabInstance.objects.filter(
                    owner_batch__name__in=[f"{PFX}-B1", f"{PFX}-B2"]).delete()
                Batch.objects.filter(name__in=[f"{PFX}-B1", f"{PFX}-B2"]).delete()
                Course.objects.filter(slug=f"{PFX}-course").delete()
                User.objects.filter(username=f"{PFX}-i1").delete()
            cleanup["deleted"] = True
        except Exception as exc:  # noqa: BLE001
            cleanup["error"] = f"{type(exc).__name__}: {exc}"
        free = IPLease.objects.filter(state="free").count()
        total = IPLease.objects.count()
        return {
            "phase": "down", "deprovision": deprov, "statuses": statuses,
            "ip_pool": {"free": free, "total": total, "full": free == total},
            "labinstances_remaining": LabInstance.objects.count(),
            "vminstances_remaining": VMInstance.objects.count(),
            "cleanup": cleanup,
            "pass": (all(s == LabInstance.Status.DESTROYED for s in statuses.values())
                     and free == total and VMInstance.objects.count() == 0
                     and LabInstance.objects.count() == 0),
        }
