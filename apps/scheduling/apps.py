from django.apps import AppConfig


class SchedulingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scheduling"

    def ready(self):
        # Connect the login-session capture signal.
        from . import signals  # noqa: F401
