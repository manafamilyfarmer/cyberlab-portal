from django.contrib import admin

from .models import IPLease, LabInstance, LabTemplate, VMInstance


@admin.register(LabTemplate)
class LabTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "role", "source_template_vmid", "is_active")
    list_filter = ("role", "is_active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(IPLease)
class IPLeaseAdmin(admin.ModelAdmin):
    list_display = ("ip", "state", "vm_instance", "leased_at", "released_at")
    list_filter = ("state",)
    search_fields = ("ip",)


@admin.register(LabInstance)
class LabInstanceAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "provisioning_mode", "lab_exercise", "lab_template", "created_at")
    list_filter = ("status", "provisioning_mode")


@admin.register(VMInstance)
class VMInstanceAdmin(admin.ModelAdmin):
    list_display = ("id", "lab_instance", "vmid", "role", "ip", "mirrored")
    list_filter = ("role", "mirrored")
