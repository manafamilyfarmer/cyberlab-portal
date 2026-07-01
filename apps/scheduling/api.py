"""scheduling API.

Read (/api/): role-filtered via get_queryset (object isolation → 404 for
out-of-scope ids). Write (/api/admin/): IsAdminOrInstructor + StaffMFARequired,
instructors limited to their own batches/students, every write audited.
AccessSession is read-only (created by the login signal).
"""
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated

from apps.accounts.permissions import IsAdminOrInstructor, StaffMFARequired
from apps.audit.services import write_audit
from apps.curriculum.models import Batch

from .models import AccessSession, LabReservation, Schedule
from .serializers import (
    AccessSessionSerializer,
    LabReservationSerializer,
    ScheduleSerializer,
)


def _profiles(request):
    user = request.user
    return (
        getattr(user, "instructor_profile", None),
        getattr(user, "student_profile", None),
    )


def _instructor_owns_batch(ip, batch):
    return batch is not None and ip is not None and batch.instructor_id == ip.id


def _instructor_owns_student(ip, student):
    return (
        student is not None
        and ip is not None
        and Batch.objects.filter(instructor=ip, students=student).exists()
    )


# --------------------------------------------------------------------------- #
# READ (role-filtered)                                                          #
# --------------------------------------------------------------------------- #
class _RoleReadViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]

    def _role(self):
        return getattr(self.request.user, "role", None)


class ScheduleViewSet(_RoleReadViewSet):
    serializer_class = ScheduleSerializer

    def get_queryset(self):
        role = self._role()
        if role == "admin":
            return Schedule.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return Schedule.objects.filter(
                Q(batch__instructor=ip) | Q(student__batches__instructor=ip)
            ).distinct()
        if role == "student" and sp is not None:
            return Schedule.objects.filter(
                Q(student=sp) | Q(batch__students=sp)
            ).distinct()
        return Schedule.objects.none()


class AccessSessionViewSet(_RoleReadViewSet):
    serializer_class = AccessSessionSerializer

    def get_queryset(self):
        role = self._role()
        if role == "admin":
            return AccessSession.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return AccessSession.objects.filter(student__batches__instructor=ip).distinct()
        if role == "student" and sp is not None:
            return AccessSession.objects.filter(student=sp)
        return AccessSession.objects.none()


class LabReservationViewSet(_RoleReadViewSet):
    serializer_class = LabReservationSerializer

    def get_queryset(self):
        role = self._role()
        if role == "admin":
            return LabReservation.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return LabReservation.objects.filter(student__batches__instructor=ip).distinct()
        if role == "student" and sp is not None:
            return LabReservation.objects.filter(student=sp)
        return LabReservation.objects.none()


# --------------------------------------------------------------------------- #
# WRITE (role-gated, MFA-gated, audited, instructor-scoped)                      #
# --------------------------------------------------------------------------- #
class AdminScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduleSerializer
    permission_classes = [IsAuthenticated, IsAdminOrInstructor, StaffMFARequired]

    def get_queryset(self):
        if getattr(self.request.user, "role", None) == "instructor":
            ip, _ = _profiles(self.request)
            if ip is None:
                return Schedule.objects.none()
            return Schedule.objects.filter(
                Q(batch__instructor=ip) | Q(student__batches__instructor=ip)
            ).distinct()
        return Schedule.objects.all()

    def _check_instructor_scope(self, batch, student):
        if getattr(self.request.user, "role", None) != "instructor":
            return
        ip, _ = _profiles(self.request)
        ok = (_instructor_owns_batch(ip, batch) if batch else
              _instructor_owns_student(ip, student))
        if not ok:
            raise PermissionDenied("Instructors may only schedule their own batches/students.")

    def perform_create(self, serializer):
        self._check_instructor_scope(
            serializer.validated_data.get("batch"),
            serializer.validated_data.get("student"),
        )
        obj = serializer.save()
        write_audit(self.request.user, "schedule.create", request=self.request,
                    target_type="Schedule", target_id=obj.pk)

    def perform_update(self, serializer):
        self._check_instructor_scope(
            serializer.validated_data.get("batch", serializer.instance.batch),
            serializer.validated_data.get("student", serializer.instance.student),
        )
        obj = serializer.save()
        write_audit(self.request.user, "schedule.update", request=self.request,
                    target_type="Schedule", target_id=obj.pk)

    def perform_destroy(self, instance):
        target_id = instance.pk
        instance.delete()
        write_audit(self.request.user, "schedule.delete", request=self.request,
                    target_type="Schedule", target_id=target_id)


class AdminLabReservationViewSet(viewsets.ModelViewSet):
    serializer_class = LabReservationSerializer
    permission_classes = [IsAuthenticated, IsAdminOrInstructor, StaffMFARequired]

    def get_queryset(self):
        if getattr(self.request.user, "role", None) == "instructor":
            ip, _ = _profiles(self.request)
            if ip is None:
                return LabReservation.objects.none()
            return LabReservation.objects.filter(student__batches__instructor=ip).distinct()
        return LabReservation.objects.all()

    def _check_instructor_scope(self, student):
        if getattr(self.request.user, "role", None) != "instructor":
            return
        ip, _ = _profiles(self.request)
        if not _instructor_owns_student(ip, student):
            raise PermissionDenied("Instructors may only reserve for students in their batches.")

    def perform_create(self, serializer):
        self._check_instructor_scope(serializer.validated_data.get("student"))
        obj = serializer.save()
        write_audit(self.request.user, "labreservation.create", request=self.request,
                    target_type="LabReservation", target_id=obj.pk)

    def perform_update(self, serializer):
        self._check_instructor_scope(
            serializer.validated_data.get("student", serializer.instance.student)
        )
        obj = serializer.save()
        write_audit(self.request.user, "labreservation.update", request=self.request,
                    target_type="LabReservation", target_id=obj.pk)

    def perform_destroy(self, instance):
        target_id = instance.pk
        instance.delete()
        write_audit(self.request.user, "labreservation.delete", request=self.request,
                    target_type="LabReservation", target_id=target_id)
