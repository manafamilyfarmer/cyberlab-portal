"""labs admin API — records only.

Templates: read = admin/instructor, write = admin (MFA-gated, audited).
IPLeases + instances: read-only visibility for admins. There is deliberately NO
create endpoint for LabInstance/VMInstance — provisioning is B2 (Celery).
"""
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.accounts.permissions import IsAdmin, IsAdminOrInstructor, StaffMFARequired
from apps.audit.services import write_audit

from .models import IPLease, LabInstance, LabTemplate
from .serializers import (
    IPLeaseSerializer,
    LabInstanceSerializer,
    LabTemplateSerializer,
)


class AdminLabTemplateViewSet(viewsets.ModelViewSet):
    """Template CRUD. Read: admin/instructor. Write: admin + verified TOTP."""

    queryset = LabTemplate.objects.all()
    serializer_class = LabTemplateSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsAdminOrInstructor()]
        return [IsAuthenticated(), IsAdmin(), StaffMFARequired()]

    def _audit(self, suffix, obj):
        write_audit(
            self.request.user,
            f"labtemplate.{suffix}",
            request=self.request,
            target_type="LabTemplate",
            target_id=obj.pk,
        )

    def perform_create(self, serializer):
        self._audit("create", serializer.save())

    def perform_update(self, serializer):
        self._audit("update", serializer.save())

    def perform_destroy(self, instance):
        target_id = instance.pk
        instance.delete()
        write_audit(
            self.request.user, "labtemplate.delete", request=self.request,
            target_type="LabTemplate", target_id=target_id,
        )


class AdminIPLeaseViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only pool status for admins (free/leased)."""

    queryset = IPLease.objects.all()
    serializer_class = IPLeaseSerializer
    permission_classes = [IsAuthenticated, IsAdmin]


class AdminLabInstanceViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only instance visibility (none exist until B2 provisioning)."""

    queryset = LabInstance.objects.all()
    serializer_class = LabInstanceSerializer
    permission_classes = [IsAuthenticated, IsAdminOrInstructor]
