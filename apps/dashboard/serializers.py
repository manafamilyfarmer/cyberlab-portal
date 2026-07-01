from rest_framework import serializers

from apps.audit.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    """Read-only projection of an audit row. Every field is read_only — this
    serializer never writes (the viewset is read-only and AuditLog is
    append-only at the model layer)."""

    actor_username = serializers.CharField(
        source="actor.username", read_only=True, default=None
    )

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "created_at",
            "actor",
            "actor_username",
            "action",
            "target_type",
            "target_id",
            "detail",
            "source_ip",
        ]
        read_only_fields = fields
