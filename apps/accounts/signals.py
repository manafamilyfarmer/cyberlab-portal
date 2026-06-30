"""Auth events → AuditLog. Connected in AccountsConfig.ready()."""
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from apps.audit.services import write_audit

try:
    from axes.signals import user_locked_out
except Exception:  # pragma: no cover - axes always present in this build
    user_locked_out = None


@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs):
    write_audit(
        user,
        "auth.login_success",
        request=request,
        role=getattr(user, "role", None),
    )


@receiver(user_login_failed)
def on_user_login_failed(sender, credentials, request=None, **kwargs):
    write_audit(
        None,
        "auth.login_failed",
        request=request,
        username=(credentials or {}).get("username"),
    )


def _on_locked_out(sender, request=None, **kwargs):
    write_audit(
        None,
        "auth.lockout",
        request=request,
        username=kwargs.get("username"),
        ip_address=kwargs.get("ip_address"),
    )


if user_locked_out is not None:
    user_locked_out.connect(_on_locked_out, dispatch_uid="accounts_axes_lockout")
