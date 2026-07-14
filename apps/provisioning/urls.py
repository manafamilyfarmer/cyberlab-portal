from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api, views

# WRITE (validate + enqueue): /api/admin/labinstances[/{id}/deprovision]
# and per-student boxes: /api/admin/student-labs/{provision,provision-batch,{id}/deprovision}
admin_router = DefaultRouter()
admin_router.register(
    "labinstances", api.AdminLabInstanceViewSet, basename="admin-provision-labinstance"
)
admin_router.register(
    "student-labs", api.AdminStudentLabViewSet, basename="admin-student-lab"
)

# READ (role-filtered): /api/labinstances
# Student's own box: GET /api/my-lab, POST /api/my-lab/{start,stop}
read_router = DefaultRouter()
read_router.register(
    "labinstances", api.LabInstanceViewSet, basename="provision-labinstance"
)
read_router.register(
    "my-lab", api.MyLabViewSet, basename="my-lab"
)

urlpatterns = [
    path("api/admin/", include(admin_router.urls)),
    path("api/", include(read_router.urls)),
    # Template (session) frontend — renders the same data as GET /api/my-lab/.
    path("my-lab/", views.my_lab, name="my-lab-page"),
]
