"""Async ClamAV quarantine gate (B0 §20).

scan_submission streams the stored file to clamd (INSTREAM) and sets
scan_status. It tolerates "clamd not ready yet" by retrying with backoff, and
falls back to scan_status=error (never crashes the pipeline) if clamd stays
unavailable. Defense-in-depth — NOT the primary upload control.
"""
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from django.conf import settings

from apps.audit.services import write_audit

from . import clamav
from .models import Submission


@shared_task(bind=True, max_retries=12, default_retry_delay=20)
def scan_submission(self, submission_id):
    try:
        sub = Submission.objects.get(pk=submission_id)
    except Submission.DoesNotExist:
        return "gone"

    try:
        with open(sub.stored_path, "rb") as fh:
            data = fh.read()
    except OSError:
        sub.scan_status = Submission.ScanStatus.ERROR
        sub.save(update_fields=["scan_status"])
        return "error:file-missing"

    try:
        result, detail = clamav.instream_scan(
            settings.CLAMAV_HOST, settings.CLAMAV_PORT, data
        )
    except ConnectionError as exc:
        # clamd not ready (still fetching signatures) → retry with backoff.
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            sub.scan_status = Submission.ScanStatus.ERROR
            sub.save(update_fields=["scan_status"])
            return "error:clamd-unavailable"

    if result == "infected":
        sub.scan_status = Submission.ScanStatus.INFECTED
        sub.quarantined = True
        sub.save(update_fields=["scan_status", "quarantined"])
        write_audit(
            None, "submission.infected", request=None,
            target_type="Submission", target_id=sub.pk, signature=detail,
        )
        return f"infected:{detail}"

    if result == "clean":
        sub.scan_status = Submission.ScanStatus.CLEAN
        sub.save(update_fields=["scan_status"])
        return "clean"

    sub.scan_status = Submission.ScanStatus.ERROR
    sub.save(update_fields=["scan_status"])
    return f"error:{detail}"
