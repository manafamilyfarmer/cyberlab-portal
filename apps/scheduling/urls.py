from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api

read_router = DefaultRouter()
read_router.register("schedules", api.ScheduleViewSet, basename="schedule")
read_router.register("sessions", api.AccessSessionViewSet, basename="session")
read_router.register("reservations", api.LabReservationViewSet, basename="reservation")

admin_router = DefaultRouter()
admin_router.register("schedules", api.AdminScheduleViewSet, basename="admin-schedule")
admin_router.register("reservations", api.AdminLabReservationViewSet, basename="admin-reservation")

urlpatterns = [
    path("api/", include(read_router.urls)),
    path("api/admin/", include(admin_router.urls)),
]
