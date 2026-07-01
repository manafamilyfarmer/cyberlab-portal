from rest_framework import serializers

from .models import Assessment, Submission


class SubmissionSerializer(serializers.ModelSerializer):
    """Read serializer — deliberately omits stored_path (internal disk path)."""

    class Meta:
        model = Submission
        fields = [
            "id", "student", "lab_exercise", "submitted_at", "original_filename",
            "content_type", "size", "sha256", "scan_status", "quarantined",
        ]
        read_only_fields = fields


class AssessmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Assessment
        fields = [
            "id", "submission", "instructor", "marks", "comments", "status", "graded_at",
        ]
        read_only_fields = ["instructor", "graded_at"]
