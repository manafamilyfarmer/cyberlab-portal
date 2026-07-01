from django.contrib import admin

from .models import AccessSession, LabReservation, Schedule


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ("id", "batch", "student", "quota_minutes_per_day", "is_active")
    list_filter = ("is_active",)


@admin.register(AccessSession)
class AccessSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "student", "login_at", "source_ip", "lab_start", "lab_stop")
    list_filter = ("login_at",)
    readonly_fields = ("login_at",)


@admin.register(LabReservation)
class LabReservationAdmin(admin.ModelAdmin):
    list_display = ("id", "student", "target_vmid", "window_start", "window_end")
