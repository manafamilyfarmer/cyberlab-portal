"""Structured JSON-lines sink for audit events — the SIEM ingestion source.

Every ``write_audit()`` call emits ONE compact JSON object per line to
``settings.AUDIT_LOG_PATH``, IN ADDITION to (never instead of) the AuditLog DB
row. A Wazuh agent / syslog / API tails that file (Wazuh part 2 wires the
transport to VM109). This module owns:

  * a dedicated logger ("cyberlab.audit") with a RotatingFileHandler + a JSON
    formatter (one JSON object per line — JSONL),
  * the stable event schema the SIEM parses,
  * a secret scrubber so token/password/key values never reach the file.

Rotation choice — RotatingFileHandler (size cap + a few backups), append mode.
The stream is written by several processes (gunicorn workers + the celery
worker + beat). Appends are line-sized and atomic on POSIX; rotation across
processes is *best-effort* (one process may rotate while another holds the old
fd — at worst a few lines land in the just-rotated file). We accept that: the
size cap is a hard disk-safety bound, and losing the exact rotation boundary
does not lose audit rows (the DB AuditLog is the authoritative ledger; this file
is the forwarding stream). The alternative — append-only + external logrotate
(copytruncate) — trades the same best-effort boundary for an ops dependency; the
in-process size cap is simpler and self-contained, so we use it.

Nothing here may raise into the caller: write_audit wraps the emit in
try/except, and this module is defensive on top of that.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

from django.conf import settings

HOST = "cyberlab-portal"
_REDACTED = "***REDACTED***"

# Any dict key whose name matches this (case-insensitive, substring) has its
# value redacted before it reaches the JSONL — even if a caller passed a secret
# into **detail by mistake. Covers token/password/secret/key/credential shapes.
_SECRET_KEY_RE = re.compile(
    r"(secret|password|passwd|pwd|token|api[-_ ]?key|_key\b|\bkey\b|credential|private)",
    re.IGNORECASE,
)

# Result is derived from the action's LAST dotted segment (separator-agnostic:
# catches ".abort", ".rejected", "login_failed" alike) plus an "error" key in
# detail. Order matters: error wins over ok.
_ERROR_TOKENS = ("error", "fail", "abort", "reject", "denied", "invalid", "partial", "timeout")

_logger = None
_logger_lock = Lock()


def _category(action: str) -> str:
    if action.startswith(("provision.", "deprovision.")):
        return "provisioning"
    if action.startswith("reaper."):
        return "reaper"
    if action.startswith("auth."):
        return "auth"
    if action.startswith("submission."):
        return "submission"
    return "admin"


def _result(action: str, detail) -> str:
    last = action.rsplit(".", 1)[-1].lower()
    if any(tok in last for tok in _ERROR_TOKENS):
        return "error"
    if isinstance(detail, dict) and "error" in detail:
        return "error"
    if last == "ok" or last.endswith("_ok") or "success" in last:
        return "ok"
    return "info"


def scrub(value):
    """Deep copy of ``value`` with any secret-looking dict key redacted.

    Recurses through dicts and lists so a nested secret (e.g.
    detail={"cfg": {"token": "..."}}) is caught too.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = scrub(v)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


class _JsonLineFormatter(logging.Formatter):
    """Render ``record.audit_event`` as one compact JSON object per line."""

    def format(self, record):
        event = getattr(record, "audit_event", None)
        if event is None:  # defensive: a stray log to this logger
            event = {"message": record.getMessage(), "host": HOST}
        return json.dumps(event, default=str, separators=(",", ":"), ensure_ascii=False)


def _get_logger():
    """Lazily build the "cyberlab.audit" logger + rotating file handler.

    delay=True so import/first-configure never fails if the path is not yet
    writable; the file is opened on the first actual emit (and re-tried on every
    emit while it stays unopened — that is what makes 4.6 recovery work).
    """
    global _logger
    if _logger is not None:
        return _logger
    with _logger_lock:
        if _logger is not None:
            return _logger
        lg = logging.getLogger("cyberlab.audit")
        lg.setLevel(logging.INFO)
        lg.propagate = False  # never leak audit lines into the root/app log
        if not lg.handlers:
            path = Path(settings.AUDIT_LOG_PATH)
            handler = RotatingFileHandler(
                str(path),
                maxBytes=int(getattr(settings, "AUDIT_LOG_MAX_BYTES", 50 * 1024 * 1024)),
                backupCount=int(getattr(settings, "AUDIT_LOG_BACKUPS", 5)),
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(_JsonLineFormatter())
            lg.addHandler(handler)
        _logger = lg
        return lg


def reset_logger_for_tests():
    """Drop the cached logger/handlers so the next emit reopens the file.

    Used by the verify driver after it changes AUDIT_LOG_PATH permissions, so the
    handler picks up the new state instead of a stale (possibly-broken) fd.
    """
    global _logger
    with _logger_lock:
        lg = logging.getLogger("cyberlab.audit")
        for h in list(lg.handlers):
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
            lg.removeHandler(h)
        _logger = None


def _iso_utc(ts) -> str:
    if ts is None:
        ts = _dt.datetime.now(_dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_event(*, actor, action, target_type, target_id, source_ip, detail, timestamp=None):
    """Build the stable, secret-scrubbed event dict the SIEM parses."""
    username = getattr(actor, "username", None) if actor is not None else None
    role = getattr(actor, "role", None) if actor is not None else None
    return {
        "@timestamp": _iso_utc(timestamp),
        "event_type": action,
        "category": _category(action),
        "actor": username or "system",
        "actor_role": role,
        "target_type": (target_type or None),
        "target_id": (str(target_id) if target_id not in (None, "") else None),
        "source_ip": (str(source_ip) if source_ip else None),
        "result": _result(action, detail),
        "detail": scrub(detail if isinstance(detail, dict) else {}),
        "host": HOST,
    }


def emit_audit_event(*, actor, action, target_type, target_id, source_ip, detail, timestamp=None):
    """Emit ONE JSONL line for an audit event. Best-effort; never raises."""
    event = build_event(
        actor=actor, action=action, target_type=target_type, target_id=target_id,
        source_ip=source_ip, detail=detail, timestamp=timestamp,
    )
    _get_logger().info("", extra={"audit_event": event})
