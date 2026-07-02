"""B2 Step 4 verification driver — exercises the SHARED provisioning model
end-to-end through the REAL API layer (DRF request factory + force_authenticate,
so the actual permission/ownership/MFA/queryset code runs) and the REAL Celery
worker (create() calls .delay(); a separate worker process executes Proxmox).

Phases:
  --phase up    : setup throwaway data -> provision (as instructor I1) -> poll to
                  running -> RBAC matrix -> capacity guard. Leaves the VM RUNNING
                  and prints {lab_id, vmid, ip} for an independent mgmt01 check.
  --phase down  : deprovision (as I1) -> poll to destroyed -> zero-residue check
                  -> delete throwaway data.

No Proxmox call is made in this command's request path — provisioning happens in
the worker. manage.py runs THROUGH entrypoint on the worker container.
"""
import json
import time

from django.conf import settings as dj_settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import InstructorProfile, StudentProfile, User
from apps.curriculum.models import Batch, Course, LabExercise, Module
from apps.labs.models import IPLease, LabInstance, VMInstance
from apps.provisioning.allocation import capacity_precheck_db
from apps.provisioning.api import AdminLabInstanceViewSet, LabInstanceViewSet

PFX = "b2s4"
POLL_CAP = 180
POLL_INTERVAL = 3.0

factory = APIRequestFactory()


def _staff(user):
    """Attach a passing MFA check so StaffMFARequired is satisfied for staff in
    the request-factory path (OTPMiddleware is not in the factory stack)."""
    user.is_verified = lambda: True
    return user


def _create_view():
    return AdminLabInstanceViewSet.as_view({"post": "create"})


def _deprovision_view():
    return AdminLabInstanceViewSet.as_view({"post": "deprovision"})


def _list_view():
    return LabInstanceViewSet.as_view({"get": "list"})


def _retrieve_view():
    return LabInstanceViewSet.as_view({"get": "retrieve"})


def setup_throwaway():
    """Idempotent throwaway world: instructor I1 owns batch B1 (student S1,
    exercise E1); batch B2 (student S2) must stay isolated."""
    course, _ = Course.objects.get_or_create(
        slug=f"{PFX}-course", defaults={"name": "B2S4 Course"})
    module, _ = Module.objects.get_or_create(
        course=course, code="B2S4", defaults={"title": "B2S4 Module"})
    e1, _ = LabExercise.objects.get_or_create(
        module=module, slug=f"{PFX}-e1",
        defaults={"title": "B2S4 Exercise E1", "submission_required": False})

    i1_user, _ = User.objects.get_or_create(
        username=f"{PFX}-i1",
        defaults={"role": User.Role.INSTRUCTOR, "email": "b2s4-i1@example.invalid"})
    i1, _ = InstructorProfile.objects.get_or_create(user=i1_user)

    s1_user, _ = User.objects.get_or_create(
        username=f"{PFX}-s1",
        defaults={"role": User.Role.STUDENT, "email": "b2s4-s1@example.invalid"})
    s1, _ = StudentProfile.objects.get_or_create(user=s1_user)
    s2_user, _ = User.objects.get_or_create(
        username=f"{PFX}-s2",
        defaults={"role": User.Role.STUDENT, "email": "b2s4-s2@example.invalid"})
    s2, _ = StudentProfile.objects.get_or_create(user=s2_user)

    b1, _ = Batch.objects.get_or_create(
        course=course, name=f"{PFX}-B1", defaults={"instructor": i1})
    if b1.instructor_id != i1.id:
        b1.instructor = i1
        b1.save(update_fields=["instructor"])
    b1.students.add(s1)

    b2, _ = Batch.objects.get_or_create(
        course=course, name=f"{PFX}-B2", defaults={"instructor": None})
    b2.students.add(s2)

    return {
        "course": course, "module": module, "e1": e1,
        "i1_user": i1_user, "i1": i1,
        "s1_user": s1_user, "s2_user": s2_user, "b1": b1, "b2": b2,
    }


class Command(BaseCommand):
    help = "B2 Step 4 shared-provisioning verification driver."

    def add_arguments(self, parser):
        parser.add_argument("--phase", choices=["up", "down"], required=True)

    def handle(self, *args, **opts):
        if opts["phase"] == "up":
            out = self._up()
        else:
            out = self._down()
        self.stdout.write("=== B2S4_JSON_BEGIN ===")
        self.stdout.write(json.dumps(out, indent=2, default=str))
        self.stdout.write("=== B2S4_JSON_END ===")

    # ---------------------------------------------------------------- up ------
    def _up(self):
        w = setup_throwaway()
        out = {"phase": "up", "setup": {
            "i1": w["i1_user"].pk, "b1": w["b1"].pk, "b2": w["b2"].pk,
            "s1": w["s1_user"].pk, "s2": w["s2_user"].pk, "e1": w["e1"].pk}}

        # --- PROVISION as I1 (owns B1) : validate + enqueue -----------------
        req = factory.post("/api/admin/labinstances",
                           {"batch": w["b1"].pk, "lab_exercise": w["e1"].pk},
                           format="json")
        force_authenticate(req, user=_staff(w["i1_user"]))
        resp = _create_view()(req)
        out["provision_http"] = resp.status_code
        if resp.status_code != 202:
            out["provision_body"] = getattr(resp, "data", None)
            out["verdict"] = "BLOCKED (enqueue failed)"
            return out
        lab_id = resp.data["id"]
        out["lab_id"] = lab_id

        # --- poll to running (worker executes the real Proxmox path) --------
        deadline = time.monotonic() + POLL_CAP
        status = None
        while time.monotonic() < deadline:
            lab = LabInstance.objects.get(pk=lab_id)
            status = lab.status
            if status in (LabInstance.Status.RUNNING, LabInstance.Status.ERROR):
                break
            time.sleep(POLL_INTERVAL)
        out["status_after_poll"] = status
        vm = VMInstance.objects.filter(lab_instance_id=lab_id).first()
        if vm:
            out["vmid"] = vm.vmid
            out["leased_ip"] = str(vm.ip.ip) if vm.ip_id else None
            out["ip_lease_state"] = vm.ip.state if vm.ip_id else None
        # --- 4.1 binding -----------------------------------------------------
        lab = LabInstance.objects.get(pk=lab_id)
        out["binding"] = {
            "owner_batch": lab.owner_batch_id, "owner_batch_expected": w["b1"].pk,
            "lab_exercise": lab.lab_exercise_id, "lab_exercise_expected": w["e1"].pk,
            "provisioning_mode": lab.provisioning_mode,
            "status": lab.status,
            "vmid_in_range": bool(vm and vm.vmid and 9000 <= vm.vmid <= 9099),
            "ip_recorded": bool(vm and vm.ip_id),
        }
        out["rbac"] = self._rbac(w, lab_id)
        out["capacity"] = self._capacity(w)
        out["verdict"] = ("SUCCESS" if status == LabInstance.Status.RUNNING
                          else f"PARTIAL (status={status})")
        return out

    # --------------------------------------------------------------- rbac -----
    def _rbac(self, w, lab_id):
        r = {}
        # S1 sees B1's instance (read-only)
        req = factory.get("/api/labinstances")
        force_authenticate(req, user=w["s1_user"])
        resp = _list_view()(req)
        ids = [i["id"] for i in resp.data]
        r["s1_list_http"] = resp.status_code
        r["s1_sees_instance"] = lab_id in ids

        # S2 does NOT see it (list) and 404 on retrieve (object isolation)
        req = factory.get("/api/labinstances")
        force_authenticate(req, user=w["s2_user"])
        resp = _list_view()(req)
        r["s2_sees_instance"] = lab_id in [i["id"] for i in resp.data]
        req = factory.get(f"/api/labinstances/{lab_id}/")
        force_authenticate(req, user=w["s2_user"])
        resp = _retrieve_view()(req, pk=lab_id)
        r["s2_retrieve_http"] = resp.status_code  # expect 404

        # S1 cannot provision (403) or deprovision (403)
        req = factory.post("/api/admin/labinstances",
                           {"batch": w["b1"].pk, "lab_exercise": w["e1"].pk},
                           format="json")
        force_authenticate(req, user=w["s1_user"])
        r["s1_provision_http"] = _create_view()(req).status_code  # 403
        req = factory.post(f"/api/admin/labinstances/{lab_id}/deprovision")
        force_authenticate(req, user=w["s1_user"])
        r["s1_deprovision_http"] = _deprovision_view()(req, pk=lab_id).status_code  # 403

        # I1 cannot provision for B2 (not owned) -> 403
        req = factory.post("/api/admin/labinstances",
                           {"batch": w["b2"].pk, "lab_exercise": w["e1"].pk},
                           format="json")
        force_authenticate(req, user=_staff(w["i1_user"]))
        r["i1_provision_b2_http"] = _create_view()(req).status_code  # 403

        r["all_pass"] = (
            r["s1_sees_instance"] and not r["s2_sees_instance"]
            and r["s2_retrieve_http"] == 404
            and r["s1_provision_http"] == 403
            and r["s1_deprovision_http"] == 403
            and r["i1_provision_b2_http"] == 403
        )
        return r

    # ----------------------------------------------------------- capacity -----
    def _capacity(self, w):
        """With the concurrency cap set to 0, a new provision is cleanly REJECTED
        (409) at the web pre-check — no crash, no Proxmox call, no instance row."""
        old = getattr(dj_settings, "PROVISION_MAX_CONCURRENT", 10)
        before = LabInstance.objects.count()
        try:
            dj_settings.PROVISION_MAX_CONCURRENT = 0
            precheck_ok, precheck_reason = capacity_precheck_db()
            req = factory.post("/api/admin/labinstances",
                               {"batch": w["b1"].pk, "lab_exercise": w["e1"].pk},
                               format="json")
            force_authenticate(req, user=_staff(w["i1_user"]))
            resp = _create_view()(req)
            http = resp.status_code
        finally:
            dj_settings.PROVISION_MAX_CONCURRENT = old
        after = LabInstance.objects.count()
        return {
            "precheck_ok": precheck_ok, "precheck_reason": precheck_reason,
            "api_http": http, "expected_http": 409,
            "no_row_created": before == after,
            "pass": (not precheck_ok) and http == 409 and before == after,
        }

    # -------------------------------------------------------------- down ------
    def _down(self):
        out = {"phase": "down"}
        try:
            i1_user = User.objects.get(username=f"{PFX}-i1")
        except User.DoesNotExist:
            out["verdict"] = "BLOCKED (no throwaway I1)"
            return out
        lab = (LabInstance.objects
               .filter(owner_batch__name=f"{PFX}-B1")
               .order_by("-created_at").first())
        if lab is None:
            out["verdict"] = "BLOCKED (no B1 instance)"
            return out
        lab_id = lab.pk
        out["lab_id"] = lab_id

        # --- DEPROVISION as I1 : validate + enqueue -------------------------
        req = factory.post(f"/api/admin/labinstances/{lab_id}/deprovision")
        force_authenticate(req, user=_staff(i1_user))
        resp = _deprovision_view()(req, pk=lab_id)
        out["deprovision_http"] = resp.status_code

        deadline = time.monotonic() + POLL_CAP
        status = None
        while time.monotonic() < deadline:
            status = LabInstance.objects.get(pk=lab_id).status
            if status in (LabInstance.Status.DESTROYED, LabInstance.Status.ERROR):
                break
            time.sleep(POLL_INTERVAL)
        out["status_after_poll"] = status
        out["vm_rows_remaining"] = VMInstance.objects.filter(lab_instance_id=lab_id).count()

        # --- zero-residue (DB side) -----------------------------------------
        free = IPLease.objects.filter(state=IPLease.State.FREE).count()
        total = IPLease.objects.count()
        out["ip_pool"] = {"free": free, "total": total, "full": free == total}

        # --- cleanup throwaway data -----------------------------------------
        out["cleanup"] = self._cleanup()
        out["verdict"] = (
            "SUCCESS" if status == LabInstance.Status.DESTROYED
            and out["vm_rows_remaining"] == 0 and out["ip_pool"]["full"]
            and out["cleanup"]["db_at_baseline"]
            else f"PARTIAL (status={status})")
        return out

    def _cleanup(self):
        c = {}
        try:
            with transaction.atomic():
                # LabInstances first (lab_exercise is PROTECT); VMInstance cascades.
                LabInstance.objects.filter(
                    owner_batch__name__in=[f"{PFX}-B1", f"{PFX}-B2"]).delete()
                Batch.objects.filter(name__in=[f"{PFX}-B1", f"{PFX}-B2"]).delete()
                Course.objects.filter(slug=f"{PFX}-course").delete()  # cascades module/exercise
                User.objects.filter(username__in=[f"{PFX}-i1", f"{PFX}-s1", f"{PFX}-s2"]).delete()
            c["deleted"] = True
        except Exception as exc:
            c["error"] = f"{type(exc).__name__}: {exc}"
        c["labinstances_remaining"] = LabInstance.objects.count()
        c["vminstances_remaining"] = VMInstance.objects.count()
        c["throwaway_users_remaining"] = User.objects.filter(username__startswith=PFX).count()
        c["db_at_baseline"] = (
            c["labinstances_remaining"] == 0
            and c["vminstances_remaining"] == 0
            and c["throwaway_users_remaining"] == 0)
        return c
