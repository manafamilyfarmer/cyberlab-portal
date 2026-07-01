"""Dashboard + read-only audit APIs.

GET /api/dashboard/     role summary (any authenticated role; admin/instructor
                        must be MFA-verified). Anonymous -> 403.
GET /api/admin/audit/   read-only, role-scoped, paginated, filterable audit log.
                        admin=all, instructor=self+their students, student=403.

The audit endpoint is a ReadOnlyModelViewSet, so create/update/delete methods
are not routed (POST/PUT/PATCH/DELETE -> 405). AuditLog is also append-only at
the model layer (save() forbids updates, delete() raises), so there is no write
path even in principle.
"""
from django.utils.dateparse import parse_datetime
from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdminOrInstructor, StaffMFARequired

from . import scoping
from .serializers import AuditLogSerializer
from .summary import build_summary


class DashboardSummaryView(APIView):
    """Role-routed summary. StaffMFARequired is a no-op for students/guests and
    enforces a verified TOTP device for admin/instructor — matching the staff
    dashboard/audit gate."""

    permission_classes = [IsAuthenticated, StaffMFARequired]

    def get(self, request):
        return Response(build_summary(request))


class AuditPagination(PageNumberPagination):
    # Scoped to this viewset only — no global DEFAULT_PAGINATION_CLASS, so the
    # existing list endpoints keep returning bare arrays.
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only, role-scoped audit trail. Students are gated out (403) by
    IsAdminOrInstructor; staff must be MFA-verified (StaffMFARequired)."""

    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated, IsAdminOrInstructor, StaffMFARequired]
    pagination_class = AuditPagination

    def get_queryset(self):
        qs = scoping.scoped_audit(self.request)
        params = self.request.query_params

        action = params.get("action")
        if action:
            qs = qs.filter(action=action)

        target_type = params.get("target_type")
        if target_type:
            qs = qs.filter(target_type=target_type)

        actor = params.get("actor")
        if actor:
            qs = qs.filter(actor_id=actor)

        since = params.get("since")
        if since:
            dt = parse_datetime(since)
            if dt is not None:
                qs = qs.filter(created_at__gte=dt)

        until = params.get("until")
        if until:
            dt = parse_datetime(until)
            if dt is not None:
                qs = qs.filter(created_at__lte=dt)

        return qs
