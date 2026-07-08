"""VMID + IP allocation and the capacity guard for shared-model provisioning.

ATOMIC reserve-then-clone (B2 Step 4a). Allocation is arbitrated by the DATABASE
so two concurrent (or retried) provisions can never claim the same VMID or IP:

  * allocate_and_reserve_vmid(lab)  -- INSERTs a reservation row (VMInstance with
      a concrete 9000-range vmid). VMInstance.vmid is UNIQUE, so a colliding
      concurrent insert raises IntegrityError; we catch it and retry the next
      free vmid. The committed reservation is what blocks another task — a bare
      "lowest free number" (check-then-act) had a race window between the read
      and the clone.
  * lease_ip()  -- claims a free IPLease under select_for_update(skip_locked=True)
      so two tasks lock+take DIFFERENT rows instead of racing for the same one.
  * capacity_precheck_db()  -- DB-only (web-safe). capacity_ok(client) -- worker
      authoritative (adds a live Proxmox 9000-range enumeration).

allocate_and_reserve_vmid / capacity_ok issue a Proxmox read → WORKER-only.
"""
from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.labs.models import IPLease, LabInstance, Role, VMInstance

from .pve import ProxmoxClient, TARGET_VMID_MAX, TARGET_VMID_MIN

DEFAULT_MAX_CONCURRENT = 10
_VMID_SLOTS = TARGET_VMID_MAX - TARGET_VMID_MIN + 1
RESERVED = "reserved"


def _source_template() -> int:
    return int(getattr(settings, "PROVISION_SOURCE_TEMPLATE", 153))


class CapacityError(RuntimeError):
    """No capacity: no free VMID, no free IP, or the concurrency cap is reached."""


def _max_concurrent() -> int:
    return int(getattr(settings, "PROVISION_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT))


# A PERSISTENT per-student box occupies a VMID/IP slot for its whole life, even
# while STOPPED — so it counts toward the student cap in every non-torn-down state.
PERSISTENT_STATUSES = (
    LabInstance.Status.PENDING,
    LabInstance.Status.RUNNING,
    LabInstance.Status.STOPPED,
)


def student_box_count(exclude_labinstance_id=None) -> int:
    """Count NON-torn-down per-student boxes (pending/running/stopped). Persistent
    boxes survive stop, so a stopped box still holds its slot."""
    qs = LabInstance.objects.filter(
        provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT,
        status__in=PERSISTENT_STATUSES,
    )
    if exclude_labinstance_id is not None:
        qs = qs.exclude(pk=exclude_labinstance_id)
    return qs.count()


def assign_student_index(student_profile) -> int:
    """Return the student's STABLE per-student index (the NN in s<NN>-kali-<vmid>),
    assigning the lowest free positive integer on first call. Idempotent: once set
    it never changes. Concurrency-safe — the student's own row is locked
    (select_for_update) and StudentProfile.student_index is UNIQUE, so two students
    racing for the same free index resolve via IntegrityError-retry."""
    from apps.accounts.models import StudentProfile

    for _ in range(50):
        try:
            with transaction.atomic():
                sp = StudentProfile.objects.select_for_update().get(
                    pk=student_profile.pk
                )
                if sp.student_index:
                    return sp.student_index
                used = set(
                    StudentProfile.objects.exclude(student_index__isnull=True)
                    .values_list("student_index", flat=True)
                )
                idx = next(i for i in range(1, 10_000) if i not in used)
                sp.student_index = idx
                sp.save(update_fields=["student_index"])
            return idx
        except IntegrityError:
            continue  # another student took this index first; recompute + retry
    raise CapacityError("could not assign a stable student_index after 50 attempts")


def _db_target_vmids() -> set[int]:
    """9000-range VMIDs already reserved/recorded on VMInstance rows."""
    return set(
        VMInstance.objects.filter(
            vmid__gte=TARGET_VMID_MIN, vmid__lte=TARGET_VMID_MAX
        ).values_list("vmid", flat=True)
    )


def _live_target_vmids(client: ProxmoxClient) -> set[int]:
    return set(client.list_target_vmids())


def active_instance_count(exclude_labinstance_id=None) -> int:
    qs = LabInstance.objects.filter(
        status__in=(LabInstance.Status.PENDING, LabInstance.Status.RUNNING)
    )
    if exclude_labinstance_id is not None:
        qs = qs.exclude(pk=exclude_labinstance_id)
    return qs.count()


def capacity_precheck_db(exclude_labinstance_id=None, *, cap=None, active=None):
    """DB-only capacity pre-check. Returns (ok, reason). WEB-SAFE (no Proxmox).

    `cap`/`active` let a caller substitute a different concurrency budget and
    counter (e.g. STUDENT_MAX_CONCURRENT + student_box_count for the per-student
    path) without changing the shared-model defaults."""
    cap = cap if cap is not None else _max_concurrent()
    active = active if active is not None else active_instance_count(exclude_labinstance_id)
    if active >= cap:
        return False, f"concurrent-instance cap reached ({active}/{cap})"
    if not IPLease.objects.filter(state=IPLease.State.FREE).exists():
        return False, "no free IP in the pool"
    if len(_db_target_vmids()) >= _VMID_SLOTS:
        return False, "no free VMID slot (DB)"
    return True, "ok"


def capacity_ok(client=None, exclude_labinstance_id=None, *, cap=None, active=None):
    """Authoritative capacity check (WORKER): DB pre-check + live Proxmox VMID
    enumeration. Returns (ok, reason)."""
    ok, reason = capacity_precheck_db(exclude_labinstance_id, cap=cap, active=active)
    if not ok:
        return ok, reason
    client = client or ProxmoxClient()
    taken = _live_target_vmids(client) | _db_target_vmids()
    if len(taken) >= _VMID_SLOTS:
        return False, "no free VMID in 9000..9099 (live)"
    return True, "ok"


def allocate_and_reserve_vmid(lab, client=None, *, role=Role.TARGET, max_attempts=None,
                              source_template=None):
    """Atomically reserve the lowest free VMID in 9000..9099 for `lab` by
    INSERTing a VMInstance reservation row (proxmox_status='reserved'). Returns
    the reserved VMInstance. The UNIQUE(vmid) constraint arbitrates: a colliding
    concurrent insert raises IntegrityError, which we catch and retry with the
    next free vmid. Raises CapacityError when the range is exhausted.

    The caller clones INTO the reserved vmid — never allocates a bare number.
    """
    client = client or ProxmoxClient()
    src_template = source_template if source_template is not None else _source_template()
    live = _live_target_vmids(client)  # one Proxmox enumeration per call
    attempts = 0
    cap = max_attempts or _VMID_SLOTS
    while attempts < cap:
        attempts += 1
        taken = live | _db_target_vmids()  # re-read committed reservations
        vmid = next(
            (c for c in range(TARGET_VMID_MIN, TARGET_VMID_MAX + 1) if c not in taken),
            None,
        )
        if vmid is None:
            raise CapacityError(f"no free VMID in {TARGET_VMID_MIN}..{TARGET_VMID_MAX}")
        try:
            with transaction.atomic():
                return VMInstance.objects.create(
                    lab_instance=lab,
                    vmid=vmid,
                    role=role,
                    proxmox_status=RESERVED,
                    source_template_vmid=src_template,
                )
        except IntegrityError:
            # Another task committed this vmid first; retry the next free one.
            continue
    raise CapacityError(
        f"could not reserve a VMID after {cap} attempts (contention/exhaustion)"
    )


def lease_ip() -> IPLease:
    """Atomically claim one free IPLease. select_for_update(skip_locked=True)
    makes two concurrent tasks lock+take DIFFERENT rows (the second skips the
    row the first locked) instead of racing for the same address. RECORD only —
    never applies an ipconfig to a VM (that is B2.3)."""
    with transaction.atomic():
        lease = (
            IPLease.objects.select_for_update(skip_locked=True)
            .filter(state=IPLease.State.FREE)
            .order_by("ip")
            .first()
        )
        if lease is None:
            raise CapacityError("no free IPLease in the pool")
        lease.state = IPLease.State.LEASED
        lease.leased_at = timezone.now()
        lease.save(update_fields=["state", "leased_at"])
        return lease


def release_lease(lease_pk) -> None:
    """Return an IPLease to the pool (leased->free). Idempotent."""
    with transaction.atomic():
        try:
            lease = IPLease.objects.select_for_update().get(pk=lease_pk)
        except IPLease.DoesNotExist:
            return
        lease.state = IPLease.State.FREE
        lease.vm_instance = None
        lease.released_at = timezone.now()
        lease.leased_at = None
        lease.save(
            update_fields=["state", "vm_instance", "released_at", "leased_at"]
        )


def release_reservation(vm) -> None:
    """Release a VMInstance reservation: free its lease(s) and delete the row.
    Idempotent / retry-safe (frees the reserved vmid for reuse)."""
    if vm is None or vm.pk is None:
        return
    with transaction.atomic():
        for pk in IPLease.objects.filter(vm_instance=vm).values_list("pk", flat=True):
            release_lease(pk)
        vm.delete()
