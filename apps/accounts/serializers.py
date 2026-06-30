from rest_framework import serializers

from .models import User


class MeSerializer(serializers.ModelSerializer):
    is_verified = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "role",
            "mfa_enabled",
            "is_staff",
            "is_superuser",
            "is_verified",
        ]
        read_only_fields = fields

    def get_is_verified(self, obj):
        is_verified = getattr(obj, "is_verified", None)
        return bool(callable(is_verified) and is_verified())
