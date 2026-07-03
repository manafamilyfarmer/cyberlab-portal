"""B2 Step 3 driver — provision from the cloud-init template (153), APPLY the
leased IP, and verify it is reachable; then teardown graceful with the IP
released AND no longer reachable.

Phases:
  --phase up   : throwaway I1 + B1 + E1 + S1; provision (as I1) -> poll to running
                 -> capture vmid, leased IP, ip_applied, reachability, the guest
                 agent's reported interfaces, and the clone's source lineage.
  --phase down : deprovision (in-worker, synchronous, to capture stop_path) ->
                 assert graceful stop -> confirm leased IP no longer reachable ->
                 cleanup throwaway. Reports zero residue.
"""
import json
import socket
import time

from django.core.management.base import BaseCommand
from django.db import transaction

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import InstructorProfile, StudentProfile, User
from apps.curriculum.models import Batch, Course, LabExercise, Module
from apps.labs.models import IPLease, LabInstance, VMInstance
from apps.provisioning.api import AdminLabInstanceViewSet
from apps.provisioning.pve import ProxmoxClient
from apps.provisioning.tasks import deprovision_instance

PFX = "b2s3"
# clone (~250s) + cloud-init first boot & agent ready (~220s) + reachability.
POLL_CAP = 660
POLL_INTERVAL = 5.0
factory = APIRequestFactory()


def _staff(u):
    u.is_verified = lambda: True
    return u


def _tcp(ip, port=22, timeout=3):
    try:
        with socket.create_connection((str(ip), port), timeout=timeout):
            return True
    except OSError:
        return False


class Command(BaseCommand):
    help = "B2 Step 3 cloud-init IP-apply + reachability driver."

    def add_arguments(self, parser):
        parser.add_argument("--phase", choices=["up", "down"], required=True)

    def handle(self, *args, **opts):
        out = self._up() if opts["phase"] == "up" else self._down()
        self.stdout.write("=== B2S3_JSON_BEGIN ===")
        self.stdout.write(json.dumps(out, indent=2, default=str))
        self.stdout.write("=== B2S3_JSON_END ===")

    def _setup(self):
        course, _ = Course.objects.get_or_create(
            slug=f"{PFX}-course", defaults={"name": "B2S3 Course"})
        module, _ = Module.objects.get_or_create(
            course=course, code="B2S3", defaults={"title": "B2S3 Module"})
        e1, _ = LabExercise.objects.get_or_create(
            module=module, slug=f"{PFX}-e1", defaults={"title": "B2S3 E1"})
        i1u, _ = User.objects.get_or_create(
            username=f"{PFX}-i1",
            defaults={"role": User.Role.INSTRUCTOR, "email": "b2s3-i1@example.invalid"})
        i1, _ = InstructorProfile.objects.get_or_create(user=i1u)
        s1u, _ = User.objects.get_or_create(
            username=f"{PFX}-s1",
            defaults={"role": User.Role.STUDENT, "email": "b2s3-s1@example.invalid"})
        s1, _ = StudentProfile.objects.get_or_create(user=s1u)
        b1, _ = Batch.objects.get_or_create(
            course=course, name=f"{PFX}-B1", defaults={"instructor": i1})
        if b1.instructor_id != i1.id:
            b1.instructor = i1
            b1.save(update_fields=["instructor"])
        b1.students.add(s1)
        return e1, i1u, b1

    def _up(self):
        e1, i1u, b1 = self._setup()
        out = {"phase": "up", "setup": {"i1": i1u.pk, "b1": b1.pk, "e1": e1.pk}}
        req = factory.post("/api/admin/labinstances",
                           {"batch": b1.pk, "lab_exercise": e1.pk}, format="json")
        force_authenticate(req, user=_staff(i1u))
        resp = AdminLabInstanceViewSet.as_view({"post": "create"})(req)
        out["provision_http"] = resp.status_code
        if resp.status_code != 202:
            out["body"] = getattr(resp, "data", None)
            out["verdict"] = "BLOCKED"
            return out
        lab_id = resp.data["id"]
        out["lab_id"] = lab_id

        deadline = time.monotonic() + POLL_CAP
        status = None
        while time.monotonic() < deadline:
            status = LabInstance.objects.get(pk=lab_id).status
            if status in (LabInstance.Status.RUNNING, LabInstance.Status.ERROR):
                break
            time.sleep(POLL_INTERVAL)
        out["status"] = status

        vm = VMInstance.objects.filter(lab_instance_id=lab_id).first()
        if vm:
            leased_ip = str(vm.ip.ip) if vm.ip_id else None
            out.update(vmid=vm.vmid, leased_ip=leased_ip,
                       ip_applied=vm.ip_applied,
                       source_template_vmid=vm.source_template_vmid)
            # live confirmations
            client = ProxmoxClient()
            cfg = client.get_config(vm.vmid)
            out["vm_ipconfig0"] = (cfg.get("data") or {}).get("ipconfig0")
            ifaces = client.agent_get_interfaces(vm.vmid)
            out["agent_ips"] = ifaces.get("ips", [])
            out["ip_in_guest"] = bool(leased_ip and leased_ip in ifaces.get("ips", []))
            out["worker_tcp_22"] = _tcp(leased_ip, 22) if leased_ip else None
        out["verdict"] = "SUCCESS" if (
            status == LabInstance.Status.RUNNING and vm and vm.ip_applied
            and out.get("ip_in_guest") and out.get("worker_tcp_22")) else f"PARTIAL ({status})"
        return out

    def _down(self):
        out = {"phase": "down"}
        lab = (LabInstance.objects.filter(owner_batch__name=f"{PFX}-B1")
               .order_by("-created_at").first())
        if lab is None:
            out["verdict"] = "BLOCKED (no B1 instance)"
            return out
        vm = lab.vms.first()
        leased_ip = str(vm.ip.ip) if vm and vm.ip_id else None
        out["lab_id"] = lab.pk
        out["leased_ip"] = leased_ip
        out["reachable_before"] = _tcp(leased_ip, 22) if leased_ip else None

        # run deprovision in-worker synchronously to capture stop_path
        res = deprovision_instance.apply(args=[lab.pk]).get()
        out["deprovision_verdict"] = res.get("verdict")
        out["vms"] = res.get("vms")
        out["stop_path"] = (res.get("vms") or [{}])[0].get("stop_path")
        out["status"] = LabInstance.objects.get(pk=lab.pk).status

        # after teardown: leased IP must no longer be reachable
        out["reachable_after"] = _tcp(leased_ip, 22, timeout=3) if leased_ip else None
        free = IPLease.objects.filter(state="free").count()
        total = IPLease.objects.count()
        out["ip_pool"] = {"free": free, "total": total, "full": free == total}

        cleanup = {}
        try:
            with transaction.atomic():
                LabInstance.objects.filter(owner_batch__name=f"{PFX}-B1").delete()
                Batch.objects.filter(name=f"{PFX}-B1").delete()
                Course.objects.filter(slug=f"{PFX}-course").delete()
                User.objects.filter(username__in=[f"{PFX}-i1", f"{PFX}-s1"]).delete()
            cleanup["deleted"] = True
        except Exception as exc:  # noqa: BLE001
            cleanup["error"] = f"{type(exc).__name__}: {exc}"
        out["cleanup"] = cleanup
        out["labinstances_remaining"] = LabInstance.objects.count()
        out["vminstances_remaining"] = VMInstance.objects.count()
        out["verdict"] = "SUCCESS" if (
            out["status"] == LabInstance.Status.DESTROYED
            and out["stop_path"] == "graceful"
            and out["reachable_after"] is False
            and out["ip_pool"]["full"]
            and out["vminstances_remaining"] == 0
            and LabInstance.objects.count() == 0) else "PARTIAL"
        return out
