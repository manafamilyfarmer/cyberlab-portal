"""Capture a portal-login AccessSession for students, decoupled from the
accounts login view via the user_logged_in signal. Connected in
SchedulingConfig.ready().
"""
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .models import AccessSession


def _session_source_ip(request):
    # No trusted-proxy list exists yet (a reverse proxy arrives at B2), so we do
    # NOT trust X-Forwarded-For — use the direct peer address only.
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR")


@receiver(user_logged_in)
def capture_student_login(sender, request, user, **kwargs):
    if getattr(user, "role", None) != "student":
        return
    profile = getattr(user, "student_profile", None)
    if profile is None:
        return  # null-safe: student without a profile
    AccessSession.objects.create(
        student=profile,
        source_ip=_session_source_ip(request),
    )
