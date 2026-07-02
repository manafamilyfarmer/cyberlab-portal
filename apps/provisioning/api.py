"""Provisioning API.

CRITICAL (CLAUDE.md): Proxmox provisioning runs ONLY inside Celery tasks, NEVER
in a web request. The endpoints here VALIDATE (role, batch ownership, MFA,
capacity pre-check) and ENQUEUE a task — they make NO Proxmox call. There is
deliberately no `import` of pve / ProxmoxClient in this module.

  * WRITE (admin/instructor, MFA-gated): POST /api/admin/labinstances,
    POST /api/admin/labinstances/{id}/deprovision. Instructors must OWN the batch.
  * READ (role-filtered): GET /api/labinstances — students see their batch(es)'
    instances (status only, no control); instructors see their batches'; admin all.
    Out-of-scope ids are simply not in the queryset -> 404 (object isolation).
"""
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdminOrInstructor, StaffMFARequired
from apps.audit.services import write_audit
from apps.curriculum.models import Batch, LabExercise
from apps.labs.models import LabInstance

from .allocation import capacity_precheck_db
from .serializers import LabInstanceSerializer
from .tasks import deprovision_instance, provision_shared_instance


def _role(user):
    return getattr(user, "role", None)


def _instructor_profile(user):
    return getattr(user, "instructor_profile", None)


def _student_profile(user):
    return getattr(user, "student_profile", None)


def _owns_batch(user, batch) -> bool:
    if _role(user) == "admin":
        return True
    ip = _instructor_profile(user)
    return ip is not None and batch is not None and batch.instructor_id == ip.id


# --------------------------------------------------------------------------- #
# WRITE — validate + enqueue only (admin/instructor, MFA-gated, batch-owned)   #
# --------------------------------------------------------------------------- #
class AdminLabInstanceViewSet(viewsets.GenericViewSet):
    """Provision / deprovision. NEVER touches Proxmox in the request — enqueues
    a Celery task that the worker executes with the portal token."""

    queryset = LabInstance.objects.all()
    serializer_class = LabInstanceSerializer
    permission_classes = [IsAuthenticated, IsAdminOrInstructor, StaffMFARequired]

    def create(self, request, *args, **kwargs):
        batch_id = request.data.get("batch")
        exercise_id = request.data.get("lab_exercise")
        if not batch_id or not exercise_id:
            return Response(
                {"detail": "Both 'batch' and 'lab_exercise' are required."},
                status=400,
            )
        try:
            batch = Batch.objects.get(pk=batch_id)
        except (Batch.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Batch not found."}, status=404)
        if not _owns_batch(request.user, batch):
            return Response(
                {"detail": "You may only provision for a batch you own."}, status=403
            )
        try:
            exercise = LabExercise.objects.get(pk=exercise_id)
        except (LabExercise.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "LabExercise not found."}, status=404)

        # DB-only capacity pre-check (NO Proxmox call in the web request).
        ok, reason = capacity_precheck_db()
        if not ok:
            write_audit(
                request.user, "provision.rejected", request=request,
                target_type="Batch", target_id=batch.pk, reason=reason,
            )
            return Response({"detail": f"Capacity: {reason}"}, status=409)

        lab = LabInstance.objects.create(
            owner_batch=batch,
            lab_exercise=exercise,
            status=LabInstance.Status.PENDING,
            provisioning_mode=LabInstance.ProvisioningMode.SHARED,
        )
        write_audit(
            request.user, "provision.enqueue", request=request,
            target_type="LabInstance", target_id=lab.pk,
            batch=batch.pk, lab_exercise=exercise.pk,
        )
        provision_shared_instance.delay(lab.pk)
        return Response(LabInstanceSerializer(lab).data, status=202)

    @action(detail=True, methods=["post"])
    def deprovision(self, request, pk=None):
        try:
            lab = LabInstance.objects.get(pk=pk)
        except (LabInstance.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Not found."}, status=404)
        if not _owns_batch(request.user, lab.owner_batch):
            return Response(
                {"detail": "You may only deprovision an instance of a batch you own."},
                status=403,
            )
        write_audit(
            request.user, "deprovision.enqueue", request=request,
            target_type="LabInstance", target_id=lab.pk,
        )
        deprovision_instance.delay(lab.pk)
        return Response(LabInstanceSerializer(lab).data, status=202)


# --------------------------------------------------------------------------- #
# READ — role-filtered, read-only (object isolation via get_queryset)          #
# --------------------------------------------------------------------------- #
class LabInstanceViewSet(viewsets.ReadOnlyModelViewSet):
    """Students: their batch(es)' instances, status only (no control). No
    provision/deprovision action exists here — students cannot write."""

    serializer_class = LabInstanceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        role = _role(user)
        if role == "admin":
            return LabInstance.objects.all()
        ip = _instructor_profile(user)
        if role == "instructor" and ip is not None:
            return LabInstance.objects.filter(owner_batch__instructor=ip).distinct()
        sp = _student_profile(user)
        if role == "student" and sp is not None:
            return LabInstance.objects.filter(owner_batch__students=sp).distinct()
        return LabInstance.objects.none()
