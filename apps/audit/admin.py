from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor", "target_type", "target_id", "source_ip")
    list_filter = ("action", "created_at")
    search_fields = ("action", "target_type", "target_id", "source_ip")
    readonly_fields = (
        "created_at", "actor", "action", "target_type", "target_id", "detail", "source_ip",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
