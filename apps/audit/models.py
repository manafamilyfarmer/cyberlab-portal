from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only audit trail.

    By convention rows are never updated or deleted from app code: save()
    refuses updates and delete() raises. There is no app-level delete path.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    action = models.CharField(max_length=128, db_index=True)
    target_type = models.CharField(max_length=128, blank=True)
    target_id = models.CharField(max_length=128, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        who = self.actor_id if self.actor_id is not None else "anon"
        return f"AuditLog<{self.action} by {who} @ {self.created_at:%Y-%m-%d %H:%M:%S}>"

    def save(self, *args, **kwargs):
        # Append-only: allow the initial insert, forbid any later update.
        if self.pk is not None:
            raise ValueError("AuditLog is append-only; updates are not allowed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("AuditLog is append-only; deletes are not allowed.")
