from django.contrib import admin

from .models import IPLease, LabInstance, LabTemplate, VMInstance, WireGuardPeer


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


@admin.register(WireGuardPeer)
class WireGuardPeerAdmin(admin.ModelAdmin):
    """Metadata-only visibility for staff (issued / downloaded / count). NEVER
    exposes key material — the model holds only a filename pointer + the client's
    PUBLIC key, and there is no admin action that reads the .conf bytes."""

    list_display = (
        "student", "tunnel_ip", "kali_ip", "active",
        "download_count", "last_downloaded_at", "issued_at",
    )
    list_filter = ("active",)
    search_fields = ("tunnel_ip", "kali_ip", "student__user__username")
    # All fields are read-only in admin: peers are managed by load_wireguard_peers,
    # not hand-edited, and nothing secret is editable here.
    readonly_fields = (
        "student", "vm_instance", "tunnel_ip", "kali_ip", "client_pubkey",
        "config_secret_ref", "issued_at", "last_downloaded_at", "download_count",
        "active", "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
