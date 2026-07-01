from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api

read_router = DefaultRouter()
read_router.register("courses", api.CourseViewSet, basename="course")
read_router.register("modules", api.ModuleViewSet, basename="module")
read_router.register("exercises", api.LabExerciseViewSet, basename="exercise")
read_router.register("batches", api.BatchViewSet, basename="batch")

admin_router = DefaultRouter()
admin_router.register("courses", api.AdminCourseViewSet, basename="admin-course")
admin_router.register("modules", api.AdminModuleViewSet, basename="admin-module")
admin_router.register("exercises", api.AdminLabExerciseViewSet, basename="admin-exercise")
admin_router.register("batches", api.AdminBatchViewSet, basename="admin-batch")

urlpatterns = [
    path("api/", include(read_router.urls)),
    path("api/admin/", include(admin_router.urls)),
]
