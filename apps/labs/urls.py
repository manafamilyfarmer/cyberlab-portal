from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api

admin_router = DefaultRouter()
admin_router.register("templates", api.AdminLabTemplateViewSet, basename="admin-labtemplate")
admin_router.register("ipleases", api.AdminIPLeaseViewSet, basename="admin-iplease")
admin_router.register("instances", api.AdminLabInstanceViewSet, basename="admin-labinstance")

urlpatterns = [
    path("api/admin/", include(admin_router.urls)),
]
