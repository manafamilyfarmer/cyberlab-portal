"""assessments — student report Submission (hostile-upload pipeline) + Assessment.

Uploaded files are treated as hostile: stored outside the web root on a
dedicated volume with a randomized name, never executed, never served inline,
and scanned by ClamAV before an instructor may download. Cross-app FKs use
string references so assessments depends on accounts/curriculum, never reverse.
"""
from django.db import models


class Submission(models.Model):
    class ScanStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        CLEAN = "clean", "Clean"
        INFECTED = "infected", "Infected"
        ERROR = "error", "Error"

    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.CASCADE, related_name="submissions"
    )
    lab_exercise = models.ForeignKey(
        "curriculum.LabExercise", on_delete=models.PROTECT, related_name="submissions"
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    stored_path = models.CharField(max_length=512)          # path on the submissions volume
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=128)          # as reported by the client
    size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    scan_status = models.CharField(
        max_length=16, choices=ScanStatus.choices, default=ScanStatus.PENDING
    )
    quarantined = models.BooleanField(default=False)

    class Meta:
        ordering = ("-submitted_at",)

    def __str__(self):
        return f"Submission<{self.pk} {self.original_filename} {self.scan_status}>"


class Assessment(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        FINAL = "final", "Final"

    submission = models.OneToOneField(
        Submission, on_delete=models.CASCADE, related_name="assessment"
    )
    instructor = models.ForeignKey(
        "accounts.InstructorProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assessments",
    )
    marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    comments = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    graded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-id",)

    def __str__(self):
        return f"Assessment<submission={self.submission_id} {self.status}>"
