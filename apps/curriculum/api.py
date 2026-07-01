"""Curriculum API.

Read viewsets (router under /api/) are role-filtered in get_queryset so
out-of-scope objects are simply not found (object-level isolation → 404).

Write viewsets (router under /api/admin/) are role-gated, MFA-gated
(StaffMFARequired), and audited (write_audit on every create/update/destroy).
"""
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import (
    IsAdmin,
    IsAdminOrInstructor,
    StaffMFARequired,
)
from apps.accounts.models import StudentProfile
from apps.audit.services import write_audit

from .models import Batch, Course, LabExercise, Module
from .permissions import CanManageBatch
from .serializers import (
    BatchSerializer,
    CourseSerializer,
    LabExerciseSerializer,
    ModuleSerializer,
)


def _profiles(request):
    user = request.user
    return (
        getattr(user, "instructor_profile", None),
        getattr(user, "student_profile", None),
    )


# --------------------------------------------------------------------------- #
# READ (role-filtered)                                                          #
# --------------------------------------------------------------------------- #
class _RoleReadViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]

    def _role(self):
        return getattr(self.request.user, "role", None)


class CourseViewSet(_RoleReadViewSet):
    serializer_class = CourseSerializer

    def get_queryset(self):
        role = self._role()
        if role == "admin":
            return Course.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return Course.objects.filter(batches__instructor=ip).distinct()
        if role == "student" and sp is not None:
            return Course.objects.filter(batches__students=sp).distinct()
        return Course.objects.none()


class ModuleViewSet(_RoleReadViewSet):
    serializer_class = ModuleSerializer

    def get_queryset(self):
        role = self._role()
        if role == "admin":
            return Module.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return Module.objects.filter(course__batches__instructor=ip).distinct()
        if role == "student" and sp is not None:
            return Module.objects.filter(course__batches__students=sp).distinct()
        return Module.objects.none()


class LabExerciseViewSet(_RoleReadViewSet):
    serializer_class = LabExerciseSerializer

    def get_queryset(self):
        role = self._role()
        if role == "admin":
            return LabExercise.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return LabExercise.objects.filter(
                module__course__batches__instructor=ip
            ).distinct()
        if role == "student" and sp is not None:
            return LabExercise.objects.filter(
                module__course__batches__students=sp
            ).distinct()
        return LabExercise.objects.none()


class BatchViewSet(_RoleReadViewSet):
    serializer_class = BatchSerializer

    def get_queryset(self):
        role = self._role()
        if role == "admin":
            return Batch.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return Batch.objects.filter(instructor=ip).distinct()
        if role == "student" and sp is not None:
            return Batch.objects.filter(students=sp).distinct()
        return Batch.objects.none()


# --------------------------------------------------------------------------- #
# WRITE (role-gated, MFA-gated, audited)                                        #
# --------------------------------------------------------------------------- #
class _AuditedModelViewSet(viewsets.ModelViewSet):
    audit_prefix = "curriculum"

    def _audit(self, action_suffix, obj):
        write_audit(
            self.request.user,
            f"{self.audit_prefix}.{action_suffix}",
            request=self.request,
            target_type=obj.__class__.__name__,
            target_id=obj.pk,
        )

    def perform_create(self, serializer):
        obj = serializer.save()
        self._audit("create", obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        self._audit("update", obj)

    def perform_destroy(self, instance):
        target_type, target_id = instance.__class__.__name__, instance.pk
        instance.delete()
        write_audit(
            self.request.user,
            f"{self.audit_prefix}.delete",
            request=self.request,
            target_type=target_type,
            target_id=target_id,
        )


class AdminCourseViewSet(_AuditedModelViewSet):
    audit_prefix = "course"
    queryset = Course.objects.all()
    serializer_class = CourseSerializer
    permission_classes = [IsAuthenticated, IsAdmin, StaffMFARequired]


class AdminModuleViewSet(_AuditedModelViewSet):
    audit_prefix = "module"
    queryset = Module.objects.all()
    serializer_class = ModuleSerializer
    permission_classes = [IsAuthenticated, IsAdmin, StaffMFARequired]


class AdminLabExerciseViewSet(_AuditedModelViewSet):
    audit_prefix = "labexercise"
    queryset = LabExercise.objects.all()
    serializer_class = LabExerciseSerializer
    permission_classes = [IsAuthenticated, IsAdmin, StaffMFARequired]


class AdminBatchViewSet(_AuditedModelViewSet):
    audit_prefix = "batch"
    queryset = Batch.objects.all()
    serializer_class = BatchSerializer
    permission_classes = [IsAuthenticated, IsAdminOrInstructor, StaffMFARequired, CanManageBatch]

    def get_queryset(self):
        # Instructors see only their own batches when LISTING; detail actions
        # resolve against all batches so CanManageBatch can return 403 (not 404)
        # for a batch they don't own.
        if self.action == "list" and getattr(self.request.user, "role", None) == "instructor":
            ip = getattr(self.request.user, "instructor_profile", None)
            return Batch.objects.filter(instructor=ip) if ip else Batch.objects.none()
        return Batch.objects.all()

    def perform_create(self, serializer):
        # A non-admin instructor always owns the batches they create.
        if getattr(self.request.user, "role", None) == "instructor":
            ip = getattr(self.request.user, "instructor_profile", None)
            obj = serializer.save(instructor=ip)
        else:
            obj = serializer.save()
        self._audit("create", obj)

    def _enroll_change(self, request, add):
        batch = self.get_object()  # enforces CanManageBatch object permission
        ids = request.data.get("student_profile_ids", [])
        profiles = list(StudentProfile.objects.filter(pk__in=ids))
        if add:
            batch.students.add(*profiles)
            suffix = "enroll"
        else:
            batch.students.remove(*profiles)
            suffix = "unenroll"
        pids = [p.pk for p in profiles]
        write_audit(
            request.user,
            f"batch.{suffix}",
            request=request,
            target_type="Batch",
            target_id=batch.pk,
            student_profile_ids=pids,
        )
        return Response({suffix: pids, "batch": batch.pk})

    @action(detail=True, methods=["post"])
    def enroll(self, request, pk=None):
        return self._enroll_change(request, add=True)

    @action(detail=True, methods=["post"])
    def unenroll(self, request, pk=None):
        return self._enroll_change(request, add=False)
