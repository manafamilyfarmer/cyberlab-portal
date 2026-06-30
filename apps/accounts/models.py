from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user with a coarse RBAC role and an MFA flag.

    Identity (email / first_name / last_name) lives on the user, not on the
    profiles. Profiles hold role-specific extras only.
    """

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        INSTRUCTOR = "instructor", "Instructor"
        STUDENT = "student", "Student"
        GUEST = "guest", "Guest"

    role = models.CharField(
        max_length=16, choices=Role.choices, default=Role.STUDENT
    )
    mfa_enabled = models.BooleanField(default=False)

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN

    @property
    def is_instructor(self):
        return self.role == self.Role.INSTRUCTOR

    @property
    def is_student(self):
        return self.role == self.Role.STUDENT

    @property
    def is_staff_role(self):
        """admin or instructor — the roles that must pass TOTP MFA."""
        return self.role in (self.Role.ADMIN, self.Role.INSTRUCTOR)


class StudentProfile(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        ALUMNI = "alumni", "Alumni"

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="student_profile"
    )
    mobile = models.CharField(max_length=32, blank=True)
    college = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    usage_quota_minutes = models.PositiveIntegerField(default=0)
    consent_pipeline = models.BooleanField(default=False)

    def __str__(self):
        return f"StudentProfile<{self.user.username}>"


class InstructorProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="instructor_profile"
    )
    department = models.CharField(max_length=255, blank=True)
    # assigned_batches M2M deferred to the curriculum step.

    def __str__(self):
        return f"InstructorProfile<{self.user.username}>"
