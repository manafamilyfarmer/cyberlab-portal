from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api

# WRITE (validate + enqueue): /api/admin/labinstances[/{id}/deprovision]
admin_router = DefaultRouter()
admin_router.register(
    "labinstances", api.AdminLabInstanceViewSet, basename="admin-provision-labinstance"
)

# READ (role-filtered): /api/labinstances
read_router = DefaultRouter()
read_router.register(
    "labinstances", api.LabInstanceViewSet, basename="provision-labinstance"
)

urlpatterns = [
    path("api/admin/", include(admin_router.urls)),
    path("api/", include(read_router.urls)),
]
