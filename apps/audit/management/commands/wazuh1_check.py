"""Wazuh part 1 proof driver — exercise + verify the JSON audit stream.

Runs INSIDE the worker container (through the entrypoint) so it writes to the
same /var/cyberlab-portal-logs volume the gunicorn web + celery worker/beat
processes share. Phases:

  --phase emit   : generate REAL audit events across categories (auth login ok +
                   failed, an admin course.create, a reaper reservation-clean),
                   plus a secret-scrub probe, a count-alignment batch (DB rows ==
                   JSONL lines), and a non-fatal/recovery probe. All go through
                   write_audit -> DB row + JSONL line.
  --phase arc-up : provision ONE throwaway shared instance via the API (enqueues
                   to the real worker) -> provision.* incl provision.ok, written
                   by the celery worker process (multi-writer proof). Polls to
                   running.
  --phase arc-down: deprovision that instance via the API (worker) -> deprovision.*
                    incl deprovision.ok; then cleanup its throwaway domain rows.
  --phase verify : read AUDIT_LOG_PATH, parse every line as JSON, and report
                   representative lines + category/result coverage + scrub proof.
  --phase cleanup: delete any throwaway domain rows left by emit/arc.
"""
import json
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.utils import timezone

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import InstructorProfile, StudentProfile, User
from apps.audit import emit as emit_mod
from apps.audit.models import AuditLog
from apps.audit.services import write_audit
from apps.curriculum.api import AdminCourseViewSet
from apps.curriculum.models import Batch, Course, LabExercise, Module
from apps.labs.models import IPLease, LabInstance, Role, VMInstance
from apps.provisioning.api import AdminLabInstanceViewSet
from apps.provisioning.reaper import reap_orphans

PFX = "wz1"
STALE_VMID = 9001
factory = APIRequestFactory()


def _staff(u):
    u.is_verified = lambda: True
    return u


def _file_lines():
    """Current line count of the JSONL file (0 if absent)."""
    path = settings.AUDIT_LOG_PATH
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


class Command(BaseCommand):
    help = "Wazuh part 1 (JSON audit stream) proof driver."

    def add_arguments(self, parser):
        parser.add_argument(
            "--phase",
            choices=["emit", "arc-up", "arc-down", "verify", "cleanup"],
            required=True)

    def handle(self, *args, **opts):
        out = getattr(self, "_" + opts["phase"].replace("-", "_"))()
        self.stdout.write("=== WZ1_JSON_BEGIN ===")
        self.stdout.write(json.dumps(out, indent=2, default=str))
        self.stdout.write("=== WZ1_JSON_END ===")

    # ------------------------------------------------------------------ emit ---
    def _emit(self):
        out = {"phase": "emit", "audit_log_path": settings.AUDIT_LOG_PATH}

        # (1) REAL auth events via the login view (fires user_logged_in / failed).
        u, _ = User.objects.get_or_create(
            username=f"{PFX}-login",
            defaults={"role": User.Role.STUDENT, "email": "wz1-login@example.invalid"})
        u.set_password("wz1-Correct-Horse-9")
        u.save(update_fields=["password"])
        c = Client()
        r_ok = c.post("/api/auth/login",
                      data=json.dumps({"username": u.username, "password": "wz1-Correct-Horse-9"}),
                      content_type="application/json")
        c2 = Client()
        r_bad = c2.post("/api/auth/login",
                        data=json.dumps({"username": u.username, "password": "wrong-pw"}),
                        content_type="application/json")
        out["auth"] = {"login_ok_http": r_ok.status_code, "login_bad_http": r_bad.status_code}

        # (2) REAL admin write: create a throwaway course (course.create).
        admin, _ = User.objects.get_or_create(
            username=f"{PFX}-admin",
            defaults={"role": User.Role.ADMIN, "email": "wz1-admin@example.invalid"})
        req = factory.post(
            "/api/admin/courses",
            {"name": "WZ1 Throwaway Course", "slug": f"{PFX}-throwaway-course",
             "track": "wz1", "description": "throwaway", "is_active": True},
            format="json")
        force_authenticate(req, user=_staff(admin))
        resp = AdminCourseViewSet.as_view({"post": "create"})(req)
        out["admin_course"] = {"http": resp.status_code}

        # (3) REAL reaper event: a stale DB reservation (no live VM) -> cleaned.
        e = self._scaffold_exercise()
        with transaction.atomic():
            lab = LabInstance.objects.create(
                lab_exercise=e, status=LabInstance.Status.PENDING,
                provisioning_mode=LabInstance.ProvisioningMode.SHARED)
            vm = VMInstance.objects.create(
                lab_instance=lab, vmid=STALE_VMID, role=Role.TARGET,
                proxmox_status="reserved", source_template_vmid=153,
                hostname="wz1-stale-9001")
            lease = (IPLease.objects.select_for_update()
                     .filter(state=IPLease.State.FREE).order_by("ip").first())
            lease.state = IPLease.State.LEASED
            lease.leased_at = timezone.now()
            lease.vm_instance = vm
            lease.save(update_fields=["state", "leased_at", "vm_instance"])
        time.sleep(2)  # age past grace=1
        reap = reap_orphans.apply(kwargs={"dry_run": False, "grace": 1}).get()
        out["reaper"] = {"reservation_cleaned": reap.get("reservation_cleaned"),
                         "vm_reaped": reap.get("vm_reaped")}

        # (4) SECRET-SCRUB probe: secrets in **detail must be redacted in JSONL.
        write_audit(None, "provision.debug", target_type="qemu", target_id=9999,
                    password="hunter2", token="tok-abc", api_key="ak-xyz",
                    db_password="pg-secret", note="ok",
                    nested={"private_key": "-----BEGIN", "keep": 1})
        out["scrub_emitted"] = True

        # (5) COUNT-ALIGNMENT: K tagged events -> K DB rows AND K JSONL lines.
        tag = f"wz1align-{int(time.time())}"
        db_before = AuditLog.objects.count()
        lines_before = _file_lines()
        K = 5
        for i in range(K):
            write_audit(None, "provision.request", target_type="align",
                        target_id=i, wz1_align=tag, i=i)
        db_after = AuditLog.objects.count()
        lines_after = _file_lines()
        out["alignment"] = {
            "K": K, "db_delta": db_after - db_before,
            "file_delta": lines_after - lines_before,
            "aligned": (db_after - db_before) == K and (lines_after - lines_before) == K,
        }

        # (6) NON-FATAL + RECOVERY: point the sink at an uncreatable path, emit
        #     (action must still succeed), then restore and emit (line lands).
        real_path = settings.AUDIT_LOG_PATH
        bad_path = os.path.join(os.path.dirname(real_path), f"nope-{tag}", "audit.jsonl")
        nonfatal = {}
        try:
            settings.AUDIT_LOG_PATH = bad_path
            emit_mod.reset_logger_for_tests()
            lines_pre = _file_lines()  # counts the REAL file (unchanged)
            row = write_audit(None, "test.nonfatal_probe", note="unwritable path")
            nonfatal["action_succeeded"] = bool(row and row.pk)
            nonfatal["real_file_unchanged"] = (_file_lines() == lines_pre)
        except Exception as exc:  # noqa: BLE001 — must NOT happen
            nonfatal["raised"] = f"{type(exc).__name__}: {exc}"
        finally:
            settings.AUDIT_LOG_PATH = real_path
            emit_mod.reset_logger_for_tests()
        lines_pre2 = _file_lines()
        write_audit(None, "test.nonfatal_recovered", note="writable again")
        nonfatal["recovered_line_written"] = (_file_lines() == lines_pre2 + 1)
        out["nonfatal"] = nonfatal

        out["file_lines_now"] = _file_lines()
        out["pass"] = (
            out["auth"]["login_ok_http"] in (200, 204)
            and out["alignment"]["aligned"]
            and nonfatal.get("action_succeeded") is True
            and nonfatal.get("real_file_unchanged") is True
            and nonfatal.get("recovered_line_written") is True
            and bool(out["reaper"]["reservation_cleaned"]))
        return out

    # ---------------------------------------------------------------- arc-up ---
    def _scaffold_exercise(self):
        course, _ = Course.objects.get_or_create(
            slug=f"{PFX}-course", defaults={"name": "WZ1 Course"})
        module, _ = Module.objects.get_or_create(
            course=course, code="WZ1", defaults={"title": "WZ1 Module"})
        e, _ = LabExercise.objects.get_or_create(
            module=module, slug=f"{PFX}-e1", defaults={"title": "WZ1 E1"})
        return e

    def _arc_setup(self):
        e = self._scaffold_exercise()
        i1u, _ = User.objects.get_or_create(
            username=f"{PFX}-i1",
            defaults={"role": User.Role.INSTRUCTOR, "email": "wz1-i1@example.invalid"})
        i1, _ = InstructorProfile.objects.get_or_create(user=i1u)
        b1, _ = Batch.objects.get_or_create(
            course=e.module.course, name=f"{PFX}-B1", defaults={"instructor": i1})
        if b1.instructor_id != i1.id:
            b1.instructor = i1
            b1.save(update_fields=["instructor"])
        return e, i1u, b1

    def _arc_up(self):
        e, i1u, b1 = self._arc_setup()
        out = {"phase": "arc-up"}
        req = factory.post("/api/admin/labinstances",
                           {"batch": b1.pk, "lab_exercise": e.pk}, format="json")
        force_authenticate(req, user=_staff(i1u))
        resp = AdminLabInstanceViewSet.as_view({"post": "create"})(req)
        out["provision_http"] = resp.status_code
        if resp.status_code != 202:
            out["body"] = getattr(resp, "data", None)
            out["verdict"] = "BLOCKED"
            return out
        lab_id = resp.data["id"]
        out["lab_id"] = lab_id
        deadline = time.monotonic() + 660
        status = None
        while time.monotonic() < deadline:
            status = LabInstance.objects.get(pk=lab_id).status
            if status in (LabInstance.Status.RUNNING, LabInstance.Status.ERROR):
                break
            time.sleep(5.0)
        out["status"] = status
        vm = VMInstance.objects.filter(lab_instance_id=lab_id).first()
        if vm:
            out.update(vmid=vm.vmid, ip_applied=vm.ip_applied)
        out["verdict"] = "SUCCESS" if status == LabInstance.Status.RUNNING else f"PARTIAL ({status})"
        return out

    def _arc_down(self):
        out = {"phase": "arc-down"}
        lab = (LabInstance.objects.filter(owner_batch__name=f"{PFX}-B1")
               .order_by("-created_at").first())
        if lab is None:
            out["verdict"] = "BLOCKED (no arc instance)"
            return out
        admin, _ = User.objects.get_or_create(
            username=f"{PFX}-admin",
            defaults={"role": User.Role.ADMIN, "email": "wz1-admin@example.invalid"})
        req = factory.post(f"/api/admin/labinstances/{lab.pk}/deprovision")
        force_authenticate(req, user=_staff(admin))
        resp = AdminLabInstanceViewSet.as_view({"post": "deprovision"})(req, pk=lab.pk)
        out["deprovision_http"] = resp.status_code
        deadline = time.monotonic() + 300
        status = None
        while time.monotonic() < deadline:
            status = LabInstance.objects.get(pk=lab.pk).status
            if status == LabInstance.Status.DESTROYED:
                break
            time.sleep(5.0)
        out["status"] = status
        free = IPLease.objects.filter(state="free").count()
        total = IPLease.objects.count()
        out["ip_pool"] = {"free": free, "total": total, "full": free == total}
        out["verdict"] = "SUCCESS" if status == LabInstance.Status.DESTROYED else f"PARTIAL ({status})"
        return out

    # --------------------------------------------------------------- verify ---
    def _verify(self):
        path = settings.AUDIT_LOG_PATH
        out = {"phase": "verify", "audit_log_path": path, "exists": os.path.exists(path)}
        events, bad = [], []
        with open(path, "r", encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    bad.append({"line": n, "err": str(exc)})
        out["total_lines"] = len(events)
        out["invalid_json_lines"] = bad
        out["valid_jsonl"] = not bad

        cats, results = {}, {}
        for ev in events:
            cats[ev.get("category")] = cats.get(ev.get("category"), 0) + 1
            results[ev.get("result")] = results.get(ev.get("result"), 0) + 1
        out["categories"] = cats
        out["results"] = results

        def _first(pred):
            for ev in reversed(events):  # most-recent first
                if pred(ev):
                    return ev
            return None

        samples = {
            "auth_ok": _first(lambda e: e.get("event_type") == "auth.login_success"),
            "auth_failed": _first(lambda e: e.get("event_type") == "auth.login_failed"),
            "admin": _first(lambda e: e.get("category") == "admin"),
            "provision": _first(lambda e: e.get("category") == "provisioning"),
            "reaper": _first(lambda e: e.get("category") == "reaper"),
            "provision_ok": _first(lambda e: e.get("event_type", "").endswith(".ok")),
        }
        out["samples"] = {k: v for k, v in samples.items() if v is not None}

        # scrub proof: the most-recent provision.debug line must redact secrets.
        scrub = _first(lambda e: e.get("event_type") == "provision.debug")
        if scrub:
            d = scrub.get("detail", {})
            blob = json.dumps(scrub)
            out["scrub"] = {
                "note_present": d.get("note") == "ok",
                "password_redacted": d.get("password") == emit_mod._REDACTED,
                "token_redacted": d.get("token") == emit_mod._REDACTED,
                "api_key_redacted": d.get("api_key") == emit_mod._REDACTED,
                "db_password_redacted": d.get("db_password") == emit_mod._REDACTED,
                "nested_private_key_redacted":
                    (d.get("nested") or {}).get("private_key") == emit_mod._REDACTED,
                "nested_keep_present": (d.get("nested") or {}).get("keep") == 1,
                "no_secret_values_in_line": all(
                    s not in blob for s in
                    ("hunter2", "tok-abc", "ak-xyz", "pg-secret", "-----BEGIN")),
            }
        out["schema_keys_ok"] = all(
            set(ev.keys()) >= {"@timestamp", "event_type", "category", "actor",
                               "actor_role", "target_type", "target_id",
                               "source_ip", "result", "detail", "host"}
            for ev in events[-50:]) if events else False

        out["pass"] = (
            out["valid_jsonl"]
            and out["schema_keys_ok"]
            and {"auth", "admin", "provisioning", "reaper"} <= set(cats)
            and {"ok", "error", "info"} <= set(results)
            and bool(out.get("scrub"))
            and all(out["scrub"].values()))
        return out

    # -------------------------------------------------------------- cleanup ---
    def _cleanup(self):
        out = {"phase": "cleanup"}
        with transaction.atomic():
            # free any lease still bound to a throwaway VMInstance / stale row
            for lease in IPLease.objects.filter(
                    state=IPLease.State.LEASED,
                    vm_instance__hostname__startswith="wz1-"):
                lease.state = IPLease.State.FREE
                lease.vm_instance = None
                lease.released_at = timezone.now()
                lease.leased_at = None
                lease.save(update_fields=["state", "vm_instance", "released_at", "leased_at"])
            LabInstance.objects.filter(lab_exercise__slug=f"{PFX}-e1").delete()
            Batch.objects.filter(name=f"{PFX}-B1").delete()
            Course.objects.filter(slug__in=[f"{PFX}-course", f"{PFX}-throwaway-course"]).delete()
            User.objects.filter(
                username__in=[f"{PFX}-login", f"{PFX}-admin", f"{PFX}-i1"]).delete()
            VMInstance.objects.filter(hostname__startswith="wz1-").delete()
        out["labinstances_remaining"] = LabInstance.objects.count()
        out["vminstances_remaining"] = VMInstance.objects.count()
        free = IPLease.objects.filter(state="free").count()
        total = IPLease.objects.count()
        out["ip_pool"] = {"free": free, "total": total, "full": free == total}
        out["pass"] = out["ip_pool"]["full"]
        return out
