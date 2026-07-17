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
    # Staff console (B6.4). NB: mounted at /instructor/, not under /admin/ —
    # the django.contrib.admin "admin/" prefix would shadow it.
    path("instructor/", views.instructor_console, name="instructor-console"),
    path(
        "instructor/student/<int:student_id>/",
        views.instructor_student_detail,
        name="instructor-student-detail",
    ),
]
