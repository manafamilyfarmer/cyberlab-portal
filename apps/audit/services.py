"""Single entry point for writing audit rows: write_audit(...).

Everything that needs to record an auditable event calls this, so the shape of
AuditLog rows stays consistent and append-only. Each call writes TWO sinks:
  1. the AuditLog DB row (authoritative ledger — unchanged behaviour), and
  2. one structured JSON line to settings.AUDIT_LOG_PATH (the SIEM stream; see
     apps.audit.emit). The JSON emit is log-and-continue: a logging failure never
     breaks the audited action.
"""
import logging

from .emit import emit_audit_event
from .models import AuditLog

_emit_logger = logging.getLogger("cyberlab.audit.emit")


def client_ip(request):
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        # left-most is the original client
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def write_audit(actor, action, request=None, target_type="", target_id="", **detail):
    """Append one AuditLog row and emit one JSON line to the SIEM stream.

    actor: a User instance or None (anonymous / unknown / system tasks).
    action: dotted event name, e.g. "auth.login_success".
    request: optional, used only to capture source_ip.
    detail: arbitrary JSON-serialisable kwargs stored in detail (secrets are
        scrubbed before they reach the JSON stream).
    """
    resolved_actor = actor if getattr(actor, "is_authenticated", False) else None
    source_ip = client_ip(request)
    row = AuditLog.objects.create(
        actor=resolved_actor,
        action=action,
        target_type=target_type or "",
        target_id="" if target_id is None else str(target_id),
        detail=detail or {},
        source_ip=source_ip,
    )
    # Second sink: the structured JSON stream. NEVER let a logging failure break
    # the audited action — the DB row above is already committed.
    try:
        emit_audit_event(
            actor=resolved_actor,
            action=action,
            target_type=row.target_type,
            target_id=row.target_id,
            source_ip=source_ip,
            detail=detail or {},
            timestamp=row.created_at,
        )
    except Exception:  # noqa: BLE001 — log-and-continue
        _emit_logger.exception("audit JSON emit failed for action=%s", action)
    return row
