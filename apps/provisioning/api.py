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
from django.conf import settings
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import StudentProfile
from apps.accounts.permissions import IsAdminOrInstructor, StaffMFARequired
from apps.audit.services import write_audit
from apps.curriculum.models import Batch, LabExercise
from apps.labs.models import LabInstance

from .allocation import capacity_precheck_db, student_box_count
from .serializers import LabInstanceSerializer
from .tasks import (
    deprovision_instance,
    deprovision_student_instance,
    provision_shared_instance,
    provision_student_instance,
    start_student_instance,
    stop_student_instance,
)

# Live (non-torn-down) states a persistent per-student box can be in.
_LIVE_STUDENT_STATUSES = (
    LabInstance.Status.PENDING,
    LabInstance.Status.RUNNING,
    LabInstance.Status.STOPPED,
)


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


def _instructor_owns_student(user, sp) -> bool:
    """Admin: any student. Instructor: only a student enrolled in a batch they own."""
    if _role(user) == "admin":
        return True
    ip = _instructor_profile(user)
    if ip is None or sp is None:
        return False
    return sp.batches.filter(instructor=ip).exists()


def _student_cap() -> int:
    return int(getattr(settings, "STUDENT_MAX_CONCURRENT", 12))


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
            # A student sees their batch(es)' shared instances AND their OWN
            # per-student box (owner_student=sp) — and nothing else (isolation).
            return LabInstance.objects.filter(
                Q(owner_batch__students=sp) | Q(owner_student=sp)
            ).distinct()
        return LabInstance.objects.none()


# --------------------------------------------------------------------------- #
# WRITE — per-student PERSISTENT box (admin/instructor, MFA-gated) B3 Step 1    #
# --------------------------------------------------------------------------- #
class AdminStudentLabViewSet(viewsets.GenericViewSet):
    """CREATE-once / bulk-create / DESTROY of per-student PERSISTENT boxes. Like
    the shared viewset it NEVER touches Proxmox in the request — it validates
    (role, student/batch ownership, MFA, capacity pre-check) and enqueues a Celery
    task. Students CANNOT reach this viewset (provision/deprovision are staff-only)."""

    queryset = LabInstance.objects.filter(
        provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT
    )
    serializer_class = LabInstanceSerializer
    permission_classes = [IsAuthenticated, IsAdminOrInstructor, StaffMFARequired]

    @action(detail=False, methods=["post"])
    def provision(self, request):
        """CREATE one student's box (idempotent in the worker — never re-clones)."""
        student_id = request.data.get("student")
        if not student_id:
            return Response({"detail": "'student' is required."}, status=400)
        try:
            sp = StudentProfile.objects.get(pk=student_id)
        except (StudentProfile.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Student not found."}, status=404)
        if not _instructor_owns_student(request.user, sp):
            return Response(
                {"detail": "You may only provision for a student in a batch you own."},
                status=403,
            )
        ok, reason = capacity_precheck_db(cap=_student_cap(), active=student_box_count())
        if not ok:
            write_audit(request.user, "student.provision.rejected", request=request,
                        target_type="StudentProfile", target_id=sp.pk, reason=reason)
            return Response({"detail": f"Capacity: {reason}"}, status=409)
        write_audit(request.user, "student.provision.enqueue", request=request,
                    target_type="StudentProfile", target_id=sp.pk)
        provision_student_instance.delay(sp.pk)
        return Response({"detail": "provision enqueued", "student": sp.pk}, status=202)

    @action(detail=False, methods=["post"], url_path="provision-batch")
    def provision_batch(self, request):
        """BULK-create: one box per enrolled student in a batch (per-batch trigger,
        NOT per-login). Each enqueue is idempotent in the worker."""
        batch_id = request.data.get("batch")
        if not batch_id:
            return Response({"detail": "'batch' is required."}, status=400)
        try:
            batch = Batch.objects.get(pk=batch_id)
        except (Batch.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Batch not found."}, status=404)
        if not _owns_batch(request.user, batch):
            return Response(
                {"detail": "You may only provision for a batch you own."}, status=403
            )
        enqueued = []
        for sp in batch.students.all():
            write_audit(request.user, "student.provision.enqueue", request=request,
                        target_type="StudentProfile", target_id=sp.pk, batch=batch.pk)
            provision_student_instance.delay(sp.pk)
            enqueued.append(sp.pk)
        return Response({"detail": "bulk provision enqueued", "batch": batch.pk,
                         "students": enqueued, "count": len(enqueued)}, status=202)

    @action(detail=True, methods=["post"])
    def deprovision(self, request, pk=None):
        """DESTROY (explicit teardown) of a student's persistent box."""
        try:
            lab = LabInstance.objects.get(
                pk=pk, provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT
            )
        except (LabInstance.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Not found."}, status=404)
        if not _instructor_owns_student(request.user, lab.owner_student):
            return Response(
                {"detail": "You may only deprovision a box of a student you own."},
                status=403,
            )
        write_audit(request.user, "student.deprovision.enqueue", request=request,
                    target_type="LabInstance", target_id=lab.pk)
        deprovision_student_instance.delay(lab.pk)
        return Response(LabInstanceSerializer(lab).data, status=202)


# --------------------------------------------------------------------------- #
# READ + START/STOP — a student's OWN box only (object isolation) B3 Step 1     #
# --------------------------------------------------------------------------- #
class MyLabViewSet(viewsets.GenericViewSet):
    """A student's single PERSISTENT box: GET it, START it (start-on-login), or
    STOP it. Strictly scoped to the caller's own box (owner_student == caller) —
    a student can never see or control another's box, and cannot provision or
    destroy (those are staff-only on AdminStudentLabViewSet)."""

    serializer_class = LabInstanceSerializer
    permission_classes = [IsAuthenticated]

    def _my_box(self, user):
        sp = _student_profile(user)
        if sp is None:
            return None
        return (LabInstance.objects
                .filter(owner_student=sp,
                        provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT,
                        status__in=_LIVE_STUDENT_STATUSES)
                .order_by("created_at")
                .first())

    def list(self, request):
        if _role(request.user) != "student":
            return Response({"detail": "Students only."}, status=403)
        box = self._my_box(request.user)
        if box is None:
            return Response({"detail": "No lab box provisioned yet."}, status=404)
        return Response(LabInstanceSerializer(box).data)

    @action(detail=False, methods=["post"])
    def start(self, request):
        if _role(request.user) != "student":
            return Response({"detail": "Students only."}, status=403)
        box = self._my_box(request.user)
        if box is None:
            return Response({"detail": "No lab box provisioned yet."}, status=404)
        write_audit(request.user, "student.start.enqueue", request=request,
                    target_type="LabInstance", target_id=box.pk)
        start_student_instance.delay(box.pk)
        return Response(LabInstanceSerializer(box).data, status=202)

    @action(detail=False, methods=["post"])
    def stop(self, request):
        if _role(request.user) != "student":
            return Response({"detail": "Students only."}, status=403)
        box = self._my_box(request.user)
        if box is None:
            return Response({"detail": "No lab box provisioned yet."}, status=404)
        write_audit(request.user, "student.stop.enqueue", request=request,
                    target_type="LabInstance", target_id=box.pk)
        stop_student_instance.delay(box.pk)
        return Response(LabInstanceSerializer(box).data, status=202)
