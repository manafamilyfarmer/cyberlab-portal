from django.db import models

from apps.accounts.models import InstructorProfile, StudentProfile


class Course(models.Model):
    """Top-level catalog unit. Admin-only creation (see api permissions)."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    track = models.CharField(max_length=64, blank=True)  # e.g. "A-Offensive-AppSec"
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Module(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="modules")
    code = models.CharField(max_length=16)  # M0/F1/C1/P1/A1/X1
    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)

    class Meta:
        unique_together = (("course", "code"),)
        ordering = ("course", "order")

    def __str__(self):
        return f"{self.course.slug}/{self.code}"


class LabExercise(models.Model):
    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name="exercises")
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    instructions = models.TextField(blank=True)
    # FREE TEXT for now — the real LabTemplate FK arrives with the labs app (B2+).
    target_descriptor = models.TextField(blank=True)
    reset_required = models.BooleanField(default=False)
    submission_required = models.BooleanField(default=False)
    max_marks = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        unique_together = (("module", "slug"),)
        ordering = ("module", "title")

    def __str__(self):
        return self.title


class Batch(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="batches")
    # Reverse accessor InstructorProfile.assigned_batches wires the relation
    # deferred in Step 3 without an accounts schema change.
    instructor = models.ForeignKey(
        InstructorProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_batches",
    )
    name = models.CharField(max_length=255)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    class_days = models.JSONField(default=list, blank=True)
    class_time = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    # Canonical enrollment (reverse: StudentProfile.batches).
    students = models.ManyToManyField(
        StudentProfile, related_name="batches", blank=True
    )

    class Meta:
        ordering = ("name",)
        verbose_name_plural = "batches"

    def __str__(self):
        return self.name
