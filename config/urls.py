from django.contrib import admin
from django.urls import include, path

from . import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", health.healthz, name="healthz"),
    path("readyz/", health.readyz, name="readyz"),
    path("", include("apps.accounts.urls")),
    path("", include("apps.curriculum.urls")),
    path("", include("apps.labs.urls")),
    path("", include("apps.scheduling.urls")),
    path("", include("apps.assessments.urls")),
]
