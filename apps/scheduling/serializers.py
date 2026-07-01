from rest_framework import serializers

from .models import AccessSession, LabReservation, Schedule


class ScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Schedule
        fields = [
            "id", "batch", "student", "allowed_days", "start_time", "end_time",
            "quota_minutes_per_day", "grace_minutes", "lab", "is_active",
        ]

    def validate(self, attrs):
        # Mirror Schedule.clean(): exactly one of batch / student.
        batch = attrs.get("batch", getattr(self.instance, "batch", None))
        student = attrs.get("student", getattr(self.instance, "student", None))
        if bool(batch) == bool(student):
            raise serializers.ValidationError(
                "Exactly one of 'batch' or 'student' must be set."
            )
        return attrs


class AccessSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccessSession
        fields = [
            "id", "student", "vm_instance", "login_at", "lab_start", "lab_stop",
            "duration_seconds", "source_ip", "extended_by",
        ]
        read_only_fields = fields  # created by the login signal / B2 lab flow


class LabReservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabReservation
        fields = ["id", "student", "target_vmid", "window_start", "window_end", "note"]
