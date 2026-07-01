from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api

read_router = DefaultRouter()
read_router.register("submissions", api.SubmissionViewSet, basename="submission")
read_router.register("assessments", api.AssessmentReadViewSet, basename="assessment")

admin_router = DefaultRouter()
admin_router.register("assessments", api.AdminAssessmentViewSet, basename="admin-assessment")

urlpatterns = [
    path("api/", include(read_router.urls)),
    path("api/admin/", include(admin_router.urls)),
]
