"""Read serializers for LabInstance / VMInstance.

These are READ-ONLY projections. Nothing here creates or controls a VM — the
provisioning write path is the admin viewset (validate + enqueue) plus the
Celery tasks. Students receive status/exercise visibility only.
"""
from rest_framework import serializers

from apps.labs.models import LabInstance, VMInstance


class VMInstanceMiniSerializer(serializers.ModelSerializer):
    ip = serializers.SerializerMethodField()

    class Meta:
        model = VMInstance
        fields = ("id", "vmid", "hostname", "role", "proxmox_status", "ip")
        read_only_fields = fields

    def get_ip(self, obj):
        # The leased address is a RECORD only (not applied to the VM until B2.3).
        return str(obj.ip.ip) if obj.ip_id else None


class LabInstanceSerializer(serializers.ModelSerializer):
    vms = VMInstanceMiniSerializer(many=True, read_only=True)

    class Meta:
        model = LabInstance
        fields = (
            "id", "owner_batch", "owner_student", "lab_exercise", "lab_template",
            "status", "provisioning_mode", "created_at", "expires_at", "vms",
        )
        read_only_fields = fields
