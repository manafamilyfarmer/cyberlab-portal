from django.contrib import admin

from .models import Assessment, Submission


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("id", "student", "lab_exercise", "original_filename", "scan_status", "quarantined", "submitted_at")
    list_filter = ("scan_status", "quarantined")
    readonly_fields = ("stored_path", "sha256", "size", "submitted_at")


@admin.register(Assessment)
class AssessmentAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "instructor", "marks", "status", "graded_at")
    list_filter = ("status",)
