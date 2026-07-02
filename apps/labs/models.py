"""labs — RECORDS ONLY.

The data model that B2 provisioning will later drive. There is deliberately NO
Proxmox interaction here: no clone/start/stop, no API calls. These are inert
records. Cross-app FKs use string references ("curriculum.LabExercise", etc.)
so labs depends on curriculum/accounts, never the reverse.
"""
from django.db import models


class Role(models.TextChoices):
    ATTACKER = "attacker", "Attacker"
    TARGET = "target", "Target"


class LabTemplate(models.Model):
    """A cloneable source image, as a record. source_template_vmid is NOT
    validated against Proxmox in B1 — it is just stored."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    source_template_vmid = models.IntegerField()
    role = models.CharField(max_length=16, choices=Role.choices)
    cores = models.PositiveIntegerField(default=1)
    ram_mb = models.PositiveIntegerField(default=1024)
    disk_gb = models.PositiveIntegerField(default=10)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class LabInstance(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        STOPPED = "stopped", "Stopped"
        EXPIRED = "expired", "Expired"
        ERROR = "error", "Error"
        DESTROYED = "destroyed", "Destroyed"

    class ProvisioningMode(models.TextChoices):
        SHARED = "shared", "Shared"
        PER_STUDENT = "per_student", "Per student"
        POD = "pod", "Pod"

    owner_student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="lab_instances",
    )
    owner_batch = models.ForeignKey(
        "curriculum.Batch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="lab_instances",
    )
    lab_exercise = models.ForeignKey(
        "curriculum.LabExercise", on_delete=models.PROTECT,
        related_name="lab_instances",
    )
    lab_template = models.ForeignKey(
        LabTemplate, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="lab_instances",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    provisioning_mode = models.CharField(
        max_length=16, choices=ProvisioningMode.choices, default=ProvisioningMode.SHARED
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"LabInstance<{self.pk} {self.status}>"


class IPLease(models.Model):
    class State(models.TextChoices):
        FREE = "free", "Free"
        LEASED = "leased", "Leased"

    ip = models.GenericIPAddressField(unique=True)
    state = models.CharField(max_length=8, choices=State.choices, default=State.FREE)
    vm_instance = models.ForeignKey(
        "labs.VMInstance", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ip_leases",
    )
    leased_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("ip",)

    def __str__(self):
        return f"{self.ip} ({self.state})"


class VMInstance(models.Model):
    lab_instance = models.ForeignKey(
        LabInstance, on_delete=models.CASCADE, related_name="vms"
    )
    vmid = models.IntegerField(null=True, blank=True)
    hostname = models.CharField(max_length=255, null=True, blank=True)
    ip = models.ForeignKey(
        IPLease, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="vms",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    proxmox_status = models.CharField(max_length=32, null=True, blank=True)
    source_template_vmid = models.IntegerField(null=True, blank=True)
    mirrored = models.BooleanField(default=False)

    class Meta:
        ordering = ("lab_instance", "role")

    def __str__(self):
        return f"VMInstance<{self.vmid or 'unassigned'} {self.role}>"
