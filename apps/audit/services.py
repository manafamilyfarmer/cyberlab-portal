"""Single entry point for writing audit rows: write_audit(...).

Everything that needs to record an auditable event calls this, so the shape of
AuditLog rows stays consistent and append-only.
"""
from .models import AuditLog


def client_ip(request):
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        # left-most is the original client
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def write_audit(actor, action, request=None, target_type="", target_id="", **detail):
    """Append one AuditLog row.

    actor: a User instance or None (anonymous / unknown).
    action: dotted event name, e.g. "auth.login_success".
    request: optional, used only to capture source_ip.
    detail: arbitrary JSON-serialisable kwargs stored in detail.
    """
    resolved_actor = actor if getattr(actor, "is_authenticated", False) else None
    return AuditLog.objects.create(
        actor=resolved_actor,
        action=action,
        target_type=target_type or "",
        target_id="" if target_id is None else str(target_id),
        detail=detail or {},
        source_ip=client_ip(request),
    )
