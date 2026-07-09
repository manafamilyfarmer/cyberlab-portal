"""labs — RECORDS ONLY.

The data model that B2 provisioning will later drive. There is deliberately NO
Proxmox interaction here: no clone/start/stop, no API calls. These are inert
records. Cross-app FKs use string references ("curriculum.LabExercise", etc.)
so labs depends on curriculum/accounts, never the reverse.
"""
from django.db import models
from django.utils import timezone


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
        constraints = [
            # B3 Step 1: at most ONE non-torn-down per-student box per student.
            # Partial unique on owner_student, scoped to per_student mode in a
            # live state (pending/running/stopped). A destroyed/expired/error box
            # frees the slot, so re-provisioning after teardown is allowed.
            models.UniqueConstraint(
                fields=["owner_student"],
                condition=models.Q(
                    provisioning_mode="per_student",
                    status__in=["pending", "running", "stopped"],
                ),
                name="uniq_active_per_student_box",
            ),
        ]

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
    # UNIQUE so the DB itself arbitrates VMID allocation: two concurrent
    # provisions inserting a reservation row for the same 9000-range vmid cannot
    # both succeed (one hits IntegrityError and retries the next free vmid).
    # Nullable is fine — Postgres treats NULLs as distinct, so unassigned rows
    # do not collide.
    vmid = models.IntegerField(null=True, blank=True, unique=True)
    hostname = models.CharField(max_length=255, null=True, blank=True)
    ip = models.ForeignKey(
        IPLease, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="vms",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    proxmox_status = models.CharField(max_length=32, null=True, blank=True)
    source_template_vmid = models.IntegerField(null=True, blank=True)
    mirrored = models.BooleanField(default=False)
    # True once cloud-init has applied the leased IP inside the guest (B2.3),
    # confirmed via the guest agent. False for a recorded-but-not-applied lease.
    ip_applied = models.BooleanField(default=False)

    class Meta:
        ordering = ("lab_instance", "role")

    def __str__(self):
        return f"VMInstance<{self.vmid or 'unassigned'} {self.role}>"


class WireGuardPeer(models.Model):
    """One WireGuard peer per student (B4.4) — a POINTER + non-secret metadata.

    CRITICAL: this row NEVER holds the private key or the raw .conf text. The
    config bytes live only in the read-only secrets bind mount; the DB stores
    ``config_secret_ref`` (the filename under the WG secrets dir, e.g.
    "student01.conf") plus non-secret fields (tunnel_ip, kali_ip, the client's
    PUBLIC key, download bookkeeping). The download endpoint streams the file by
    this pointer and never logs the bytes.

    The peer is bound to the student's persistent Kali box: ``kali_ip`` is the
    box's leased IP and ``vm_instance`` points at that VMInstance. The loader
    (load_wireguard_peers) validates tunnel_ip↔kali_ip↔VMInstance consistency
    from manifest.tsv — it never reads the private-key-bearing .conf.
    """

    student = models.OneToOneField(
        "accounts.StudentProfile", on_delete=models.CASCADE,
        related_name="wireguard_peer",
    )
    vm_instance = models.ForeignKey(
        "labs.VMInstance", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="wireguard_peers",
    )
    # Tunnel-side address inside the WG overlay (e.g. 10.13.13.150). Unique.
    tunnel_ip = models.GenericIPAddressField(protocol="IPv4", unique=True)
    # The student's Kali box address on the lab network (e.g. 192.168.100.150).
    kali_ip = models.GenericIPAddressField(protocol="IPv4")
    # The client's WireGuard PUBLIC key — non-secret (base64, 44 chars).
    client_pubkey = models.CharField(max_length=64)
    # POINTER only: filename/relative path of the .conf under the WG secrets dir.
    # NOT the config contents. Resolved against settings.WG_SECRETS_DIR at
    # download time; a basename is enforced so it can never traverse the dir.
    config_secret_ref = models.CharField(max_length=255)
    # First time the config was actually handed out (set on first download).
    issued_at = models.DateTimeField(null=True, blank=True)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)
    download_count = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("tunnel_ip",)

    def __str__(self):
        return f"WireGuardPeer<{self.student_id} {self.tunnel_ip}>"


class ReaperSighting(models.Model):
    """First-seen stamp for an un-ageable 9000-range orphan (reaper v2).

    The orphan reaper reaps a 9000-range VM only when it has NO active DB
    reservation AND it is older than the grace window. Age normally comes from
    uptime / the qmclone task / the config ctime — but if NONE of those resolve,
    the reaper must not skip the orphan forever (that was how an un-ageable orphan
    could linger indefinitely). Instead it records a first-seen stamp here on the
    first sweep; on a LATER sweep, once (now - first_seen) >= grace, the orphan is
    reaped. Net effect: an un-ageable orphan is reaped within ~2 grace windows and
    can never linger forever, while a box with an active reservation (every pilot
    box) is never stamped and never touched. The row is deleted when the vmid is
    reaped or regains a reservation, so it self-cleans.
    """

    vmid = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    first_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("vmid",)

    def __str__(self):
        return f"ReaperSighting<{self.vmid} @ {self.first_seen:%Y-%m-%dT%H:%M:%S}>"
