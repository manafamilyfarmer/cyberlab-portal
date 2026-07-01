from rest_framework import serializers

from .models import IPLease, LabInstance, LabTemplate, VMInstance


class LabTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabTemplate
        fields = [
            "id", "name", "slug", "source_template_vmid", "role",
            "cores", "ram_mb", "disk_gb", "description", "is_active",
        ]


class IPLeaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = IPLease
        fields = ["id", "ip", "state", "vm_instance", "leased_at", "released_at"]
        read_only_fields = fields  # pool is managed by seed_ip_pool / B2, not the API


class VMInstanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = VMInstance
        fields = [
            "id", "lab_instance", "vmid", "hostname", "ip", "role",
            "proxmox_status", "source_template_vmid", "mirrored",
        ]
        read_only_fields = fields


class LabInstanceSerializer(serializers.ModelSerializer):
    vms = VMInstanceSerializer(many=True, read_only=True)

    class Meta:
        model = LabInstance
        fields = [
            "id", "owner_student", "owner_batch", "lab_exercise", "lab_template",
            "status", "provisioning_mode", "created_at", "expires_at", "vms",
        ]
        read_only_fields = fields  # instances are created by B2 provisioning, not here
