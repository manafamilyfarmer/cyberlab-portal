from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import InstructorProfile, StudentProfile, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "role", "mfa_enabled", "is_staff", "is_superuser")
    list_filter = ("role", "mfa_enabled", "is_staff", "is_superuser", "is_active")
    fieldsets = UserAdmin.fieldsets + (
        ("CyberLab", {"fields": ("role", "mfa_enabled")}),
    )


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "college", "status", "usage_quota_minutes", "consent_pipeline")
    list_filter = ("status", "consent_pipeline")


@admin.register(InstructorProfile)
class InstructorProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "department")
