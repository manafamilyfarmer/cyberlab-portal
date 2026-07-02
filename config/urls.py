from django.contrib import admin
from django.urls import include, path

from . import health

urlpatterns = [
    path("healthz/", health.healthz, name="healthz"),
    path("readyz/", health.readyz, name="readyz"),
    # dashboard BEFORE the admin site: it defines /admin/audit/, which the
    # django.contrib.admin "admin/" prefix would otherwise shadow.
    path("", include("apps.dashboard.urls")),
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.curriculum.urls")),
    path("", include("apps.labs.urls")),
    path("", include("apps.provisioning.urls")),
    path("", include("apps.scheduling.urls")),
    path("", include("apps.assessments.urls")),
]
