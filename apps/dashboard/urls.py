from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api, views

audit_router = DefaultRouter()
audit_router.register("audit", api.AuditLogViewSet, basename="audit")

urlpatterns = [
    # JSON APIs
    path("api/dashboard/", api.DashboardSummaryView.as_view(), name="api-dashboard"),
    path("api/admin/", include(audit_router.urls)),  # -> /api/admin/audit/
    # Template (session) frontend
    path("dashboard/", views.dashboard, name="dashboard-page"),
    path("admin/audit/", views.audit_page, name="audit-page"),
]
