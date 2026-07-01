from rest_framework import serializers

from .models import Batch, Course, LabExercise, Module


class CourseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ["id", "name", "slug", "track", "description", "is_active"]


class ModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Module
        fields = ["id", "course", "code", "title", "order", "description"]


class LabExerciseSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabExercise
        fields = [
            "id",
            "module",
            "title",
            "slug",
            "instructions",
            "target_descriptor",
            "reset_required",
            "submission_required",
            "max_marks",
        ]


class BatchSerializer(serializers.ModelSerializer):
    students = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Batch
        fields = [
            "id",
            "course",
            "instructor",
            "name",
            "start_date",
            "end_date",
            "class_days",
            "class_time",
            "is_active",
            "students",
        ]
        # instructor is assigned server-side for non-admins (perform_create);
        # enrollment is managed via the enroll/unenroll actions.
        read_only_fields = ["students"]
