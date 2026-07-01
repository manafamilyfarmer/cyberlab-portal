"""scheduling — B1 "scheduling display" slice.

Records + display only. Nothing here enforces quotas/windows against live labs
and nothing calls Proxmox (that wires in at B2). Cross-app FKs use string
references so scheduling depends on accounts/curriculum/labs, never the reverse.
"""
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Schedule(models.Model):
    """Allowed windows + daily quota, targeting EITHER a batch OR a student."""

    batch = models.ForeignKey(
        "curriculum.Batch", on_delete=models.CASCADE, null=True, blank=True,
        related_name="schedules",
    )
    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.CASCADE, null=True, blank=True,
        related_name="schedules",
    )
    allowed_days = models.JSONField(default=list, blank=True)  # e.g. ["Mon","Wed"]
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    quota_minutes_per_day = models.PositiveIntegerField(default=0)
    grace_minutes = models.PositiveIntegerField(default=0)
    lab = models.ForeignKey(
        "curriculum.LabExercise", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="schedules",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-id",)

    def clean(self):
        # Exactly one of batch / student must be set.
        if bool(self.batch_id) == bool(self.student_id):
            raise ValidationError("Exactly one of 'batch' or 'student' must be set.")

    def __str__(self):
        target = f"batch={self.batch_id}" if self.batch_id else f"student={self.student_id}"
        return f"Schedule<{target}>"


class AccessSession(models.Model):
    """A portal-login / lab session record (created by the login signal now;
    lab_start/stop populated by the B2 lab flow later)."""

    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.CASCADE, related_name="access_sessions"
    )
    vm_instance = models.ForeignKey(
        "labs.VMInstance", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="access_sessions",
    )
    login_at = models.DateTimeField(default=timezone.now)
    lab_start = models.DateTimeField(null=True, blank=True)
    lab_stop = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    extended_by = models.ForeignKey(
        "accounts.InstructorProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="extended_sessions",
    )

    class Meta:
        ordering = ("-login_at",)

    def __str__(self):
        return f"AccessSession<{self.student_id} @ {self.login_at:%Y-%m-%d %H:%M}>"


class LabReservation(models.Model):
    """Shared-target contention record (B0 §10) — display only in B1."""

    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.CASCADE, related_name="reservations"
    )
    target_vmid = models.IntegerField()
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    note = models.TextField(blank=True)

    class Meta:
        ordering = ("window_start",)

    def __str__(self):
        return f"LabReservation<{self.student_id} vmid={self.target_vmid}>"
