"""B2 Step 1 — the clone PRIMITIVE, run inside the Celery worker.

provision_clone_primitive() proves the portal can, using its OWN scoped token
(portal-pve.env) and NOT a web request:

  1. assert at RUNTIME that the token is refused (403) on a NEVER_TOUCH VM,
  2. clone template 151 -> 9000 (full clone) into the cyberlab-agent pool,
  3. read it back (exists, pooled, powered OFF — never started),
  4. record LabInstance / VMInstance / IPLease,
  5. DESTROY 9000 and release the lease, leaving ZERO residue.

The VM is never powered on, no IP is injected, and it is never bound to a
student — those are later B2 steps.

The DB is empty at B1, so a minimal Course/Module/LabExercise/LabTemplate
scaffold is created to satisfy the (non-null) LabInstance.lab_exercise FK; every
row created here is deleted again during teardown.
"""
from __future__ import annotations

import logging
import socket
import time

from celery import shared_task
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import StudentProfile
from apps.audit.services import write_audit
from apps.curriculum.models import Course, LabExercise, Module
from apps.labs.models import IPLease, LabInstance, LabTemplate, Role, VMInstance

from .allocation import (
    PERSISTENT_STATUSES,
    allocate_and_reserve_vmid,
    assign_student_index,
    capacity_ok,
    lease_ip,
    release_lease,
    release_reservation,
    student_box_count,
)
from .pve import (
    ProxmoxClient,
    ProxmoxAPIError,
    TARGET_VMID_MAX,
    TARGET_VMID_MIN,
)

logger = logging.getLogger("apps.provisioning.tasks")

CLONE_NAME = "b2-clone-probe-9000"


@shared_task(bind=True)
def provision_clone_primitive(self, source_vmid: int = 151, target_vmid: int = 9000):
    result: dict = {
        "source_vmid": source_vmid,
        "target_vmid": target_vmid,
        "aborted": None,
        "steps": {},
    }
    steps = result["steps"]
    client = ProxmoxClient()
    result["tls_verify"] = client.verify

    # --- STEP 1: RUNTIME NEGATIVE TEST FIRST (scope must be proven live) ------
    neg_code, _ = client.raw_get_status(109)
    steps["negative_test_109"] = {"http": neg_code, "expected": 403,
                                  "pass": neg_code == 403}
    logger.warning("runtime negative test: raw_get_status(109) -> HTTP %s (expect 403)",
                   neg_code)
    if neg_code != 403:
        write_audit(None, "provision.scope.abort", target_type="qemu",
                    target_id=109, http=neg_code,
                    reason="portal token NOT refused on NEVER_TOUCH 109")
        result["aborted"] = f"runtime negative test returned {neg_code}, expected 403"
        return result

    # --- STEP 2: guarded idempotency — 9000 must be absent --------------------
    pre = client.get_status(target_vmid)
    steps["preflight_9000"] = pre
    if pre.get("exists"):
        write_audit(None, "provision.clone.abort", target_type="qemu",
                    target_id=target_vmid, reason="target already exists")
        result["aborted"] = f"target {target_vmid} already exists; refusing to clobber"
        return result

    write_audit(None, "provision.clone.start", target_type="qemu",
                target_id=target_vmid, source=source_vmid, name=CLONE_NAME)

    created = {"course": None, "module": None, "exercise": None,
               "template": None, "lab_instance": None, "vm_instance": None,
               "lease": None}
    clone_done = False
    try:
        # --- STEP 4: full clone 151 -> 9000 into the pool --------------------
        clone_upid = client.clone(source_vmid, target_vmid, CLONE_NAME,
                                  full=True, pool=client.pool)
        steps["clone_upid"] = clone_upid
        task_status = client.wait_task(clone_upid)
        steps["clone_task"] = {"status": task_status.get("status"),
                               "exitstatus": task_status.get("exitstatus")}
        clone_done = True

        # --- STEP 5: read back — exists, pooled, powered OFF -----------------
        cfg = client.get_config(target_vmid)
        st = client.get_status(target_vmid)
        vm_status = (st.get("data") or {}).get("status")
        steps["readback"] = {
            "config_http": cfg.get("http"),
            "exists": cfg.get("exists"),
            "name": (cfg.get("data") or {}).get("name"),
            "power_status": vm_status,
            "powered_off": vm_status == "stopped",
        }
        if not cfg.get("exists"):
            raise ProxmoxAPIError("read-back failed: 9000 not present after clone")
        if vm_status != "stopped":
            raise ProxmoxAPIError(
                f"read-back: 9000 power status={vm_status!r}, expected 'stopped' "
                "(clone must NOT be powered on)"
            )

        # --- STEP 6: record LabInstance / VMInstance / IPLease ---------------
        with transaction.atomic():
            course = Course.objects.create(
                name="B2 Probe Course", slug="b2-probe-course",
                description="transient scaffold for B2 clone probe; deleted at teardown")
            module = Module.objects.create(course=course, code="B2", title="B2 Probe Module")
            exercise = LabExercise.objects.create(
                module=module, title="B2 Clone Probe", slug="b2-clone-probe")
            template = LabTemplate.objects.create(
                name="B2 Probe Ubuntu (151)", slug="b2-probe-ubuntu-151",
                source_template_vmid=source_vmid, role=Role.TARGET,
                description="transient scaffold for B2 clone probe; deleted at teardown")
            created.update(course=course, module=module, exercise=exercise,
                           template=template)

            lease = (IPLease.objects.select_for_update()
                     .filter(state=IPLease.State.FREE).order_by("ip").first())
            if lease is None:
                raise ProxmoxAPIError("no free IPLease available to lease")
            lab = LabInstance.objects.create(
                lab_exercise=exercise, lab_template=template,
                status="provisioned",
                provisioning_mode=LabInstance.ProvisioningMode.SHARED)
            vm = VMInstance.objects.create(
                lab_instance=lab, vmid=target_vmid, ip=lease, role=Role.TARGET,
                proxmox_status=vm_status, source_template_vmid=source_vmid,
                hostname=CLONE_NAME)
            lease.state = IPLease.State.LEASED
            lease.vm_instance = vm
            lease.leased_at = timezone.now()
            lease.save(update_fields=["state", "vm_instance", "leased_at"])
            created.update(lab_instance=lab, vm_instance=vm, lease=lease)

        steps["records"] = {
            "lab_instance_id": created["lab_instance"].pk,
            "vm_instance_id": created["vm_instance"].pk,
            "leased_ip": str(created["lease"].ip),
        }
        write_audit(None, "provision.clone.ok", target_type="qemu",
                    target_id=target_vmid, ip=str(created["lease"].ip),
                    lab_instance_id=created["lab_instance"].pk)

    except Exception as exc:  # ensure teardown even on mid-run failure
        logger.exception("provision_clone_primitive failed; entering teardown")
        result["error"] = f"{type(exc).__name__}: {exc}"

    # --- STEP 7: TEARDOWN — leave ZERO residue -------------------------------
    teardown = {}
    # 7a. destroy the VM if the clone got created
    if clone_done:
        try:
            still = client.get_status(target_vmid)
            if still.get("exists"):
                destroy_upid = client.destroy(target_vmid, purge=True)
                teardown["destroy_upid"] = destroy_upid
                dstat = client.wait_task(destroy_upid)
                teardown["destroy_task"] = {"status": dstat.get("status"),
                                            "exitstatus": dstat.get("exitstatus")}
            after = client.get_status(target_vmid)
            teardown["gone"] = not after.get("exists")
            teardown["after_http"] = after.get("http")
        except Exception as exc:
            teardown["destroy_error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("teardown destroy failed")

    # 7b. release the lease + delete DB rows we created (reverse order)
    try:
        with transaction.atomic():
            lease = created.get("lease")
            if lease is not None:
                lease.state = IPLease.State.FREE
                lease.vm_instance = None
                lease.released_at = timezone.now()
                lease.leased_at = None
                lease.save(update_fields=["state", "vm_instance",
                                          "released_at", "leased_at"])
            for key in ("vm_instance", "lab_instance", "template",
                        "exercise", "module", "course"):
                obj = created.get(key)
                if obj is not None and obj.pk is not None:
                    obj.delete()
        teardown["db_cleaned"] = True
    except Exception as exc:
        teardown["db_error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("teardown db cleanup failed")

    result["teardown"] = teardown
    if teardown.get("gone") and teardown.get("db_cleaned") and not result.get("error"):
        write_audit(None, "provision.destroy.ok", target_type="qemu",
                    target_id=target_vmid)
        result["verdict"] = "SUCCESS"
    else:
        result["verdict"] = "PARTIAL"
    return result


# ---------------------------------------------------------------------------
# B2 Step 2 — POWER LIFECYCLE probe
# ---------------------------------------------------------------------------
LIFECYCLE_NAME = "b2-lifecycle-9000"

# HARD caps (seconds). Every wait is bounded; a breach FAILS CLEANLY into the
# force-stop + destroy teardown so a stuck VM can never hang the worker or leave
# residue.
CAP_START_RUNNING = 120   # start -> running
CAP_GRACE_STOP = 90       # graceful shutdown -> stopped
CAP_FORCE_STOP = 30       # force stop -> stopped
CAP_TASK = 300            # destroy task poll cap
# Full clones on shared storage slow down under PARALLEL provisioning (I/O
# contention roughly doubles clone time), so the clone wait cap is larger than
# the destroy cap. Still bounded — a breach fails cleanly into error-path reap.
# Raised 600->900 for B3 batch provisioning: a full clone of the heavier Kali
# template (154) onto lab2-vm measured ~9-10 min under batch I/O contention,
# close enough to the old 600s cap to trip it; 900s restores headroom.
CAP_CLONE = 900           # clone task poll cap (batch/parallel-provision aware)


@shared_task(bind=True)
def provision_lifecycle_probe(self, source_vmid: int = 151, target_vmid: int = 9000):
    """Prove the portal can power a clone ON then OFF safely, with bounded waits
    and a graceful->force stop fallback, then destroy to ZERO residue. Never
    injects an IP (B2.3) or binds a student (B2.4)."""
    result: dict = {
        "source_vmid": source_vmid,
        "target_vmid": target_vmid,
        "aborted": None,
        "caps": {"start_running": CAP_START_RUNNING, "grace_stop": CAP_GRACE_STOP,
                 "force_stop": CAP_FORCE_STOP, "task": CAP_TASK},
        "steps": {},
    }
    steps = result["steps"]
    client = ProxmoxClient()
    result["tls_verify"] = client.verify

    # --- STEP 1: RUNTIME NEGATIVE TEST FIRST ---------------------------------
    neg_code, _ = client.raw_get_status(109)
    steps["negative_test_109"] = {"http": neg_code, "expected": 403,
                                  "pass": neg_code == 403}
    logger.warning("runtime negative test: raw_get_status(109) -> HTTP %s (expect 403)",
                   neg_code)
    if neg_code != 403:
        write_audit(None, "provision.scope.abort", target_type="qemu",
                    target_id=109, http=neg_code,
                    reason="portal token NOT refused on NEVER_TOUCH 109")
        result["aborted"] = f"runtime negative test returned {neg_code}, expected 403"
        return result

    # --- STEP 2: guarded idempotency -----------------------------------------
    pre = client.get_status(target_vmid)
    steps["preflight_9000"] = pre
    if pre.get("exists"):
        write_audit(None, "provision.clone.abort", target_type="qemu",
                    target_id=target_vmid, reason="target already exists")
        result["aborted"] = f"target {target_vmid} already exists; refusing to clobber"
        return result

    created = {"course": None, "module": None, "exercise": None,
               "template": None, "lab_instance": None, "vm_instance": None,
               "lease": None}
    clone_done = False
    try:
        # --- STEP 3: clone 151 -> 9000, read back stopped -------------------
        write_audit(None, "provision.clone.start", target_type="qemu",
                    target_id=target_vmid, source=source_vmid, name=LIFECYCLE_NAME)
        clone_upid = client.clone(source_vmid, target_vmid, LIFECYCLE_NAME,
                                  full=True, pool=client.pool)
        steps["clone_upid"] = clone_upid
        cstat = client.wait_task(clone_upid, timeout=CAP_TASK)
        steps["clone_task"] = {"status": cstat.get("status"),
                               "exitstatus": cstat.get("exitstatus")}
        clone_done = True

        cfg = client.get_config(target_vmid)
        st = client.get_status(target_vmid)
        vm_status = (st.get("data") or {}).get("status")
        steps["readback"] = {"exists": cfg.get("exists"), "power_status": vm_status,
                             "name": (cfg.get("data") or {}).get("name"),
                             "powered_off": vm_status == "stopped"}
        if not cfg.get("exists"):
            raise ProxmoxAPIError("read-back failed: 9000 not present after clone")
        if vm_status != "stopped":
            raise ProxmoxAPIError(f"read-back: 9000 status={vm_status!r}, expected stopped")

        # --- STEP 3b: record LabInstance / VMInstance / IPLease -------------
        with transaction.atomic():
            course = Course.objects.create(
                name="B2 Lifecycle Course", slug="b2-lifecycle-course",
                description="transient scaffold for B2 lifecycle probe; deleted at teardown")
            module = Module.objects.create(course=course, code="B2L", title="B2 Lifecycle Module")
            exercise = LabExercise.objects.create(
                module=module, title="B2 Lifecycle Probe", slug="b2-lifecycle-probe")
            template = LabTemplate.objects.create(
                name="B2 Lifecycle Ubuntu (151)", slug="b2-lifecycle-ubuntu-151",
                source_template_vmid=source_vmid, role=Role.TARGET,
                description="transient scaffold for B2 lifecycle probe; deleted at teardown")
            created.update(course=course, module=module, exercise=exercise, template=template)
            lease = (IPLease.objects.select_for_update()
                     .filter(state=IPLease.State.FREE).order_by("ip").first())
            if lease is None:
                raise ProxmoxAPIError("no free IPLease available to lease")
            lab = LabInstance.objects.create(
                lab_exercise=exercise, lab_template=template, status="provisioned",
                provisioning_mode=LabInstance.ProvisioningMode.SHARED)
            vm = VMInstance.objects.create(
                lab_instance=lab, vmid=target_vmid, ip=lease, role=Role.TARGET,
                proxmox_status=vm_status, source_template_vmid=source_vmid,
                hostname=LIFECYCLE_NAME)
            lease.state = IPLease.State.LEASED
            lease.vm_instance = vm
            lease.leased_at = timezone.now()
            lease.save(update_fields=["state", "vm_instance", "leased_at"])
            created.update(lab_instance=lab, vm_instance=vm, lease=lease)
        steps["records"] = {"lab_instance_id": lab.pk, "vm_instance_id": vm.pk,
                            "leased_ip": str(lease.ip)}

        # --- STEP 4: START -> confirm genuinely RUNNING (bounded) -----------
        write_audit(None, "provision.start", target_type="qemu", target_id=target_vmid)
        start_upid = client.start(target_vmid)
        steps["start_upid"] = start_upid
        run_wait = client.wait_status(target_vmid, "running", timeout=CAP_START_RUNNING)
        steps["start_wait"] = run_wait
        if not run_wait.get("reached"):
            raise ProxmoxAPIError(
                f"start cap breached: 9000 not 'running' within {CAP_START_RUNNING}s "
                f"(last status={run_wait.get('status')!r})")
        # liveness: guest-agent ping if available; else status==running is the proof
        steps["guest_ping"] = client.guest_ping(target_vmid)
        write_audit(None, "provision.start.ok", target_type="qemu", target_id=target_vmid,
                    waited_s=run_wait.get("waited_s"))

        # --- STEP 5: STOP (graceful first, force fallback), bounded ---------
        write_audit(None, "provision.stop", target_type="qemu", target_id=target_vmid)
        shutdown_upid = client.shutdown(target_vmid)
        steps["shutdown_upid"] = shutdown_upid
        grace = client.wait_status(target_vmid, "stopped", timeout=CAP_GRACE_STOP)
        steps["graceful_stop_wait"] = grace
        if grace.get("reached"):
            steps["stop_path"] = "graceful"
        else:
            logger.warning("graceful stop cap breached (%ss); forcing stop", CAP_GRACE_STOP)
            force_upid = client.stop(target_vmid)  # force
            steps["force_stop_upid"] = force_upid
            forced = client.wait_status(target_vmid, "stopped", timeout=CAP_FORCE_STOP)
            steps["forced_stop_wait"] = forced
            steps["stop_path"] = "forced"
            if not forced.get("reached"):
                raise ProxmoxAPIError(
                    f"force stop cap breached: 9000 not 'stopped' within {CAP_FORCE_STOP}s")
        write_audit(None, "provision.stop.ok", target_type="qemu", target_id=target_vmid,
                    stop_path=steps["stop_path"])

    except Exception as exc:  # ensure teardown even on mid-run failure / cap breach
        logger.exception("provision_lifecycle_probe failed; entering teardown")
        result["error"] = f"{type(exc).__name__}: {exc}"

    # --- STEP 6/7: TEARDOWN (force-stop if needed) -> ZERO residue -----------
    teardown = {}
    if clone_done:
        try:
            still = client.get_status(target_vmid)
            if still.get("exists"):
                cur = (still.get("data") or {}).get("status")
                # a VM left running (e.g. mid-failure) MUST be force-stopped first
                if cur == "running":
                    logger.warning("teardown: 9000 still running -> force stop")
                    fu = client.stop(target_vmid)
                    teardown["teardown_force_stop_upid"] = fu
                    teardown["teardown_force_stop_wait"] = client.wait_status(
                        target_vmid, "stopped", timeout=CAP_FORCE_STOP)
                write_audit(None, "provision.destroy.start", target_type="qemu",
                            target_id=target_vmid)
                destroy_upid = client.destroy(target_vmid, purge=True)
                teardown["destroy_upid"] = destroy_upid
                dstat = client.wait_task(destroy_upid, timeout=CAP_TASK)
                teardown["destroy_task"] = {"status": dstat.get("status"),
                                            "exitstatus": dstat.get("exitstatus")}
            after = client.get_status(target_vmid)
            teardown["gone"] = not after.get("exists")
            teardown["after_http"] = after.get("http")
        except Exception as exc:
            teardown["destroy_error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("teardown destroy failed")

    try:
        with transaction.atomic():
            lease = created.get("lease")
            if lease is not None:
                lease.state = IPLease.State.FREE
                lease.vm_instance = None
                lease.released_at = timezone.now()
                lease.leased_at = None
                lease.save(update_fields=["state", "vm_instance", "released_at", "leased_at"])
            for key in ("vm_instance", "lab_instance", "template", "exercise", "module", "course"):
                obj = created.get(key)
                if obj is not None and obj.pk is not None:
                    obj.delete()
        teardown["db_cleaned"] = True
    except Exception as exc:
        teardown["db_error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("teardown db cleanup failed")

    result["teardown"] = teardown
    if teardown.get("gone") and teardown.get("db_cleaned") and not result.get("error"):
        write_audit(None, "provision.destroy.ok", target_type="qemu", target_id=target_vmid)
        result["verdict"] = "SUCCESS"
    else:
        result["verdict"] = "PARTIAL" if not result.get("error") else "FAILED"
    return result


# ---------------------------------------------------------------------------
# B2 Step 4 — SHARED-model provisioning bound to a BATCH
# ---------------------------------------------------------------------------
# The web layer only VALIDATEs + ENQUEUEs; THESE tasks are the only place a real
# provision/deprovision happens, and they run in the Celery WORKER via the portal
# token over verified TLS. B2.3: the source is now the cloud-init template (153)
# and the leased IP is APPLIED via ipconfig0 + confirmed inside the guest.
# cloud-init first boot on a full clone is slow: the qemu-guest-agent only
# becomes answerable ~220s after the VM reports 'running' (measured on 153), so
# the apply-confirm cap is generous. Still bounded — a breach fails cleanly into
# the error-path reap (zero residue).
# Raised 300->600 for B3 batch provisioning on the heavier Kali template (154):
# when a fresh box boots while several sibling boxes contend for shared storage
# I/O, cloud-init + guest-agent IP reporting can exceed 300s (measured: the 7
# first-wave boxes confirmed in 30-105s, but later boxes tripped the 300s cap).
CAP_IP_APPLIED = 600   # poll agent until leased IP appears inside the guest
CAP_REACHABLE = 30     # worker TCP-connect to leased_ip:22


def _source_template() -> int:
    return int(getattr(settings, "PROVISION_SOURCE_TEMPLATE", 153))


def _tcp_reachable(ip, port=22, *, cap=CAP_REACHABLE, interval=2.0):
    """Poll a TCP connect to ip:port until it succeeds or the cap elapses.
    Preferred over ICMP — the worker container often lacks CAP_NET_RAW for ping.
    Returns {reachable, waited_s, port}."""
    start = time.monotonic()
    deadline = start + cap
    last_err = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((str(ip), port), timeout=3):
                return {"reachable": True, "port": port,
                        "waited_s": round(time.monotonic() - start, 1)}
        except OSError as exc:
            last_err = str(exc)
            time.sleep(interval)
    return {"reachable": False, "port": port, "error": last_err,
            "waited_s": round(time.monotonic() - start, 1)}


@shared_task(bind=True)
def provision_shared_instance(self, labinstance_id: int):
    """Provision ONE shared VM for a (batch, lab_exercise) LabInstance, ATOMIC
    reserve-then-clone (B2 Step 4a):

      capacity guard -> RESERVE vmid (unique-arbitrated) + RESERVE ip (skip_locked)
      -> clone 151 INTO the reserved vmid -> start (bounded) -> record running.

    Concurrency-safe: two parallel tasks can never reserve the same vmid or ip.
    Retry-safe: if the lab already has a running VM this is a no-op; a failure
    runs the error-path teardown (force stop -> destroy -> release reservation +
    lease) leaving ZERO residue and sets status=error, so a retry starts clean.
    The leased IP is RECORDED only (never applied as an ipconfig — that is B2.3).
    """
    result: dict = {"labinstance_id": labinstance_id, "steps": {}}
    steps = result["steps"]

    try:
        lab = (LabInstance.objects
               .select_related("owner_batch", "lab_exercise")
               .get(pk=labinstance_id))
    except LabInstance.DoesNotExist:
        result["error"] = f"LabInstance {labinstance_id} not found"
        result["verdict"] = "FAILED"
        return result

    # --- IDEMPOTENCY: a retry of an already-provisioned lab is a NO-OP --------
    existing_running = lab.vms.filter(proxmox_status="running").first()
    if existing_running is not None:
        result["vmid"] = existing_running.vmid
        result["ip"] = str(existing_running.ip.ip) if existing_running.ip_id else None
        result["idempotent_noop"] = True
        result["verdict"] = "SUCCESS"
        return result

    client = ProxmoxClient()
    result["tls_verify"] = client.verify
    batch_id = getattr(lab.owner_batch, "pk", None)
    write_audit(None, "provision.request", target_type="LabInstance",
                target_id=lab.pk, batch=batch_id,
                lab_exercise=getattr(lab.lab_exercise, "pk", None),
                mode=lab.provisioning_mode)

    # --- capacity guard (authoritative; exclude self — already counted) -------
    ok, reason = capacity_ok(client=client, exclude_labinstance_id=lab.pk)
    steps["capacity"] = {"ok": ok, "reason": reason}
    if not ok:
        lab.status = LabInstance.Status.ERROR
        lab.save(update_fields=["status"])
        write_audit(None, "provision.rejected", target_type="LabInstance",
                    target_id=lab.pk, reason=reason)
        result["error"] = f"capacity: {reason}"
        result["verdict"] = "REJECTED"
        return result

    vm = None
    lease = None
    vmid = None
    clone_done = False
    try:
        # --- RESERVE vmid (unique-arbitrated) + ip (skip_locked) FIRST -------
        vm = allocate_and_reserve_vmid(lab, client=client)
        vmid = vm.vmid
        result["vmid"] = vmid
        steps["reserved_vmid"] = vmid
        lease = lease_ip()
        steps["reserved_ip"] = str(lease.ip)
        name = f"b2-batch{batch_id}-{vmid}"
        with transaction.atomic():
            vm.ip = lease
            vm.hostname = name
            vm.save(update_fields=["ip", "hostname"])
            lease.vm_instance = vm
            lease.save(update_fields=["vm_instance"])
        write_audit(None, "provision.reserve", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk, ip=str(lease.ip))

        # --- clone 153 -> the RESERVED vmid (bounded task poll) -------------
        source_vmid = _source_template()
        leased_ip = str(lease.ip)
        write_audit(None, "provision.clone.start", target_type="qemu",
                    target_id=vmid, source=source_vmid,
                    labinstance_id=lab.pk, name=name)
        clone_upid = client.clone(source_vmid, vmid, name,
                                  full=True, pool=client.pool)
        steps["clone_upid"] = clone_upid
        cstat = client.wait_task(clone_upid, timeout=CAP_CLONE)
        steps["clone_task"] = {"status": cstat.get("status"),
                               "exitstatus": cstat.get("exitstatus")}
        clone_done = True

        # --- APPLY the leased IP via cloud-init BEFORE start ----------------
        gw = getattr(settings, "PROVISION_IP_GATEWAY", "192.168.100.1")
        cidr = int(getattr(settings, "PROVISION_IP_CIDR", 24))
        steps["set_ipconfig"] = client.set_ipconfig(vmid, leased_ip, gw=gw, cidr=cidr)
        write_audit(None, "provision.ip_set", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk, ipconfig=steps["set_ipconfig"]["ipconfig"])

        # --- START -> confirm genuinely RUNNING (bounded) -------------------
        write_audit(None, "provision.start", target_type="qemu",
                    target_id=vmid, labinstance_id=lab.pk)
        start_upid = client.start(vmid)
        steps["start_upid"] = start_upid
        run_wait = client.wait_status(vmid, "running", timeout=CAP_START_RUNNING)
        steps["start_wait"] = run_wait
        if not run_wait.get("reached"):
            raise ProxmoxAPIError(
                f"start cap breached: {vmid} not 'running' within "
                f"{CAP_START_RUNNING}s (last={run_wait.get('status')!r})")

        # --- APPLY-CONFIRM: poll the guest agent until the leased IP appears
        #     on an interface INSIDE the guest (cloud-init first boot is slow) --
        apply_start = time.monotonic()
        apply_deadline = apply_start + CAP_IP_APPLIED
        ip_in_guest = False
        last_ifaces = None
        while time.monotonic() < apply_deadline:
            ifaces = client.agent_get_interfaces(vmid)
            last_ifaces = ifaces
            if ifaces.get("ok") and leased_ip in ifaces.get("ips", []):
                ip_in_guest = True
                break
            time.sleep(3)
        steps["apply_confirm"] = {
            "ip_in_guest": ip_in_guest,
            "agent_ips": (last_ifaces or {}).get("ips", []),
            "waited_s": round(time.monotonic() - apply_start, 1),
        }
        if not ip_in_guest:
            raise ProxmoxAPIError(
                f"IP-apply not confirmed: leased {leased_ip} not present on a "
                f"guest interface within {CAP_IP_APPLIED}s "
                f"(agent said {(last_ifaces or {}).get('ips')})")
        write_audit(None, "provision.ip_applied", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk, ip=leased_ip,
                    waited_s=steps["apply_confirm"]["waited_s"])

        # --- REACHABILITY: worker TCP-connect to leased_ip:22 (openssh) -----
        reach = _tcp_reachable(leased_ip, 22)
        steps["reachable"] = reach
        write_audit(None, "provision.reachable", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk, ip=leased_ip,
                    reachable=reach.get("reachable"), port=22,
                    waited_s=reach.get("waited_s"))

        # --- record RUNNING + ip_applied (VM row exists from the reservation)
        with transaction.atomic():
            vm.proxmox_status = "running"
            vm.ip_applied = True
            vm.save(update_fields=["proxmox_status", "ip_applied"])
            lab.status = LabInstance.Status.RUNNING
            lab.save(update_fields=["status"])
        steps["vm_instance_id"] = vm.pk
        result["ip"] = leased_ip
        result["ip_applied"] = True
        result["reachable"] = reach.get("reachable")

        write_audit(None, "provision.ok", target_type="LabInstance",
                    target_id=lab.pk, vmid=vmid, ip=leased_ip,
                    source=source_vmid, waited_s=run_wait.get("waited_s"),
                    ip_applied=True, reachable=reach.get("reachable"))
        result["verdict"] = "SUCCESS"
        return result

    except Exception as exc:
        logger.exception("provision_shared_instance failed; entering error-path teardown")
        result["error"] = f"{type(exc).__name__}: {exc}"

    # --- ERROR PATH: teardown -> ZERO residue --------------------------------
    # Because we RESERVED the vmid up-front, the error path knows exactly which
    # VM to reap even if the failure was a clone TIMEOUT (clone_done False but the
    # clone may still have created the VM). So we reap the reserved vmid whenever
    # it is set — not only when clone_done. If a still-running clone holds a lock,
    # the destroy fails cleanly and is captured (reaper follow-up covers that).
    teardown: dict = {}
    if vmid is not None:
        # let an in-flight clone settle so the VM isn't lock-held during destroy
        if not clone_done and steps.get("clone_upid"):
            try:
                client.wait_task(steps["clone_upid"], timeout=CAP_CLONE)
            except Exception:  # noqa: BLE001 — best-effort; destroy handles the rest
                pass
        try:
            still = client.get_status(vmid)
            if still.get("exists"):
                cur = (still.get("data") or {}).get("status")
                if cur == "running":
                    logger.warning("error-path: %s still running -> force stop", vmid)
                    teardown["force_stop_upid"] = client.stop(vmid)
                    teardown["force_stop_wait"] = client.wait_status(
                        vmid, "stopped", timeout=CAP_FORCE_STOP)
                write_audit(None, "provision.destroy.start", target_type="qemu",
                            target_id=vmid, labinstance_id=lab.pk)
                du = client.destroy(vmid, purge=True)
                teardown["destroy_upid"] = du
                dstat = client.wait_task(du, timeout=CAP_TASK)
                teardown["destroy_task"] = {"status": dstat.get("status"),
                                            "exitstatus": dstat.get("exitstatus")}
            after = client.get_status(vmid)
            teardown["gone"] = not after.get("exists")
            teardown["after_http"] = after.get("http")
        except Exception as texc:
            teardown["destroy_error"] = f"{type(texc).__name__}: {texc}"
            logger.exception("error-path destroy failed")

    # release the reservation (frees vmid + its lease) so a retry starts clean
    try:
        release_reservation(vm)
        lab.status = LabInstance.Status.ERROR
        lab.save(update_fields=["status"])
        teardown["db_cleaned"] = True
    except Exception as texc:
        teardown["db_error"] = f"{type(texc).__name__}: {texc}"
        logger.exception("error-path db cleanup failed")

    result["teardown"] = teardown
    write_audit(None, "provision.error", target_type="LabInstance",
                target_id=lab.pk, error=result.get("error"),
                gone=teardown.get("gone"))
    result["verdict"] = "FAILED"
    return result


@shared_task(bind=True)
def deprovision_instance(self, labinstance_id: int):
    """Tear down every VM of a LabInstance: graceful shutdown (bounded) -> force
    fallback -> destroy -> release lease(s); set status=destroyed. Idempotent /
    retry-safe (a VM already gone just releases its lease and deletes the row)."""
    result: dict = {"labinstance_id": labinstance_id, "vms": []}

    try:
        lab = LabInstance.objects.get(pk=labinstance_id)
    except LabInstance.DoesNotExist:
        result["error"] = f"LabInstance {labinstance_id} not found"
        result["verdict"] = "FAILED"
        return result

    client = ProxmoxClient()
    result["tls_verify"] = client.verify
    write_audit(None, "deprovision.start", target_type="LabInstance",
                target_id=lab.pk)

    all_gone = True
    for vm in list(lab.vms.all()):
        v: dict = {"vm_instance_id": vm.pk, "vmid": vm.vmid}
        try:
            if vm.vmid is not None:
                st = client.get_status(vm.vmid)
                if st.get("exists"):
                    cur = (st.get("data") or {}).get("status")
                    if cur == "running":
                        client.shutdown(vm.vmid)
                        grace = client.wait_status(vm.vmid, "stopped",
                                                   timeout=CAP_GRACE_STOP)
                        v["graceful_stop_wait"] = grace
                        if grace.get("reached"):
                            v["stop_path"] = "graceful"
                        else:
                            logger.warning("deprovision: %s graceful cap breached -> force",
                                           vm.vmid)
                            client.stop(vm.vmid)
                            v["forced_stop_wait"] = client.wait_status(
                                vm.vmid, "stopped", timeout=CAP_FORCE_STOP)
                            v["stop_path"] = "forced"
                        write_audit(None, "deprovision.stopped", target_type="qemu",
                                    target_id=vm.vmid, labinstance_id=lab.pk,
                                    stop_path=v.get("stop_path"))
                    write_audit(None, "deprovision.destroy.start", target_type="qemu",
                                target_id=vm.vmid, labinstance_id=lab.pk)
                    du = client.destroy(vm.vmid, purge=True)
                    v["destroy_upid"] = du
                    dstat = client.wait_task(du, timeout=CAP_TASK)
                    v["destroy_task"] = {"status": dstat.get("status"),
                                         "exitstatus": dstat.get("exitstatus")}
                after = client.get_status(vm.vmid)
                v["gone"] = not after.get("exists")
                all_gone = all_gone and v["gone"]

            # release lease(s) + delete the VM row (even if it had no vmid)
            with transaction.atomic():
                lease_pks = list(
                    IPLease.objects.filter(vm_instance=vm).values_list("pk", flat=True)
                )
                for pk in lease_pks:
                    release_lease(pk)
                vm.delete()
            v["released_leases"] = lease_pks
        except Exception as exc:
            v["error"] = f"{type(exc).__name__}: {exc}"
            all_gone = False
            logger.exception("deprovision of vmid %s failed", vm.vmid)
        result["vms"].append(v)

    if all_gone:
        lab.status = LabInstance.Status.DESTROYED
        lab.save(update_fields=["status"])
        write_audit(None, "deprovision.ok", target_type="LabInstance",
                    target_id=lab.pk)
        result["verdict"] = "SUCCESS"
    else:
        write_audit(None, "deprovision.partial", target_type="LabInstance",
                    target_id=lab.pk)
        result["verdict"] = "PARTIAL"
    result["status"] = lab.status
    return result


# ---------------------------------------------------------------------------
# B3 Step 1 — PER-STUDENT PERSISTENT box (one Kali box per student, kept)
# ---------------------------------------------------------------------------
# Lifecycle: CREATE once (provision_student_instance, admin/instructor trigger —
# NOT at login) -> PERSIST (survives sessions) -> START-ON-LOGIN (start if
# stopped, idempotent, never re-clone) -> STOP (explicit) -> DESTROY (explicit
# teardown only). Source = template 154 (Kali), FULL clone onto lab2-vm, sized to
# STUDENT_RAM_MB / STUDENT_CORES. The box name s<NN>-kali-<vmid> deliberately does
# NOT carry the reaper's "b2-" prefix and always has an active DB reservation, so
# the orphan reaper can never touch it.


def _student_source_template() -> int:
    return int(getattr(settings, "STUDENT_SOURCE_TEMPLATE", 154))


def _student_max_concurrent() -> int:
    return int(getattr(settings, "STUDENT_MAX_CONCURRENT", 12))


def _assert_persistent_mode():
    """SEAM 2 gate: only the persistent pilot path is implemented. The 'ephemeral'
    scale path is a deliberate NOT-IMPLEMENTED stub."""
    mode = getattr(settings, "LIFECYCLE_MODE", "persistent")
    if mode != "persistent":
        raise NotImplementedError(
            f"LIFECYCLE_MODE={mode!r}: only 'persistent' is implemented (B3 Step 1). "
            "The 'ephemeral' scale path is a deliberate stub — do not build it here."
        )
    return mode


def _student_anchor():
    """Get-or-create the STABLE Course/Module/LabExercise/LabTemplate anchor for
    the per-student Kali box. Unlike the B2 probe scaffolds these are PERMANENT:
    persistent boxes reference them for the whole pilot, so they are NEVER deleted
    at teardown."""
    course, _ = Course.objects.get_or_create(
        slug="per-student-kali-lab",
        defaults={"name": "Per-Student Kali Lab",
                  "description": "Anchor for B3 persistent per-student Kali boxes."})
    module, _ = Module.objects.get_or_create(
        course=course, code="B3",
        defaults={"title": "Per-Student Kali Box"})
    exercise, _ = LabExercise.objects.get_or_create(
        module=module, slug="per-student-kali",
        defaults={"title": "Per-Student Kali Box"})
    template, _ = LabTemplate.objects.get_or_create(
        slug="per-student-kali-154",
        defaults={"name": "Kali (154) per-student",
                  "source_template_vmid": _student_source_template(),
                  "role": Role.ATTACKER,
                  "cores": int(getattr(settings, "STUDENT_CORES", 2)),
                  "ram_mb": int(getattr(settings, "STUDENT_RAM_MB", 4096)),
                  "description": "Per-student persistent Kali box source template."})
    return course, module, exercise, template


def _existing_student_box(sp):
    """The student's current non-torn-down per-student box, or None. Enforces the
    create-once contract in code (the DB partial-unique constraint enforces it at
    the storage layer)."""
    return (LabInstance.objects
            .filter(owner_student=sp,
                    provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT,
                    status__in=PERSISTENT_STATUSES)
            .order_by("created_at")
            .first())


# Bounded self-heal: how many times a provision may reap an orphan blocking its
# chosen VMID and retry (the next free vmid) before giving up. The 9006 wedge
# needed exactly this — a stale orphan config the allocator couldn't see made the
# clone fail "config file already exists" forever. REQUIRED, not optional.
CLONE_SELF_HEAL_ATTEMPTS = 3


def _active_reservation_vmids_excluding(vm):
    """9000-range VMIDs held by an ACTIVE DB reservation OTHER than `vm` itself.
    A vmid in this set is a REAL box (or another in-flight reservation) and must
    NEVER be reaped by the self-heal — only the caller's own just-made reservation
    is excluded so an orphan at that vmid can be cleared."""
    qs = VMInstance.objects.filter(vmid__gte=TARGET_VMID_MIN, vmid__lte=TARGET_VMID_MAX)
    if vm is not None and vm.pk is not None:
        qs = qs.exclude(pk=vm.pk)
    return set(qs.values_list("vmid", flat=True))


def _reap_orphan_vmid(client, vmid, *, labinstance_id, context):
    """Guarded self-heal: destroy an ORPHAN occupying `vmid` (present on Proxmox —
    as a VM and/or a lingering config — with no active reservation but the caller's
    own). Force-stops if running. Config-existence aware so a stale `.conf` that
    made a clone fail "config file already exists" is actually cleared. Audited.
    Returns True if the vmid is gone (no VM AND no config) afterward.

    The caller MUST have already verified `vmid` is not in
    _active_reservation_vmids_excluding(vm) — this function does not re-check
    ownership, it only relies on _guard() to keep the destroy inside 9000-9099."""
    st = client.get_status(vmid)                  # guarded
    cfg = client.get_config(vmid)                 # guarded
    if not (st.get("exists") or cfg.get("exists")):
        return True                               # nothing there to clear
    cur = (st.get("data") or {}).get("status")
    if cur == "running":
        logger.warning("self-heal: orphan %s running -> force stop", vmid)
        client.stop(vmid)                         # guarded force stop
        client.wait_status(vmid, "stopped", timeout=CAP_FORCE_STOP)
    try:
        du = client.destroy(vmid, purge=True)     # guarded
        client.wait_task(du, timeout=CAP_TASK)
    except ProxmoxAPIError:
        logger.exception("self-heal destroy of orphan %s failed", vmid)
    gone = not (client.get_status(vmid).get("exists")
                or client.get_config(vmid).get("exists"))
    write_audit(None, "provision.self_heal.reaped", target_type="qemu",
                target_id=vmid, labinstance_id=labinstance_id, gone=gone,
                reason=f"orphan blocking provision ({context})")
    logger.warning("self-heal reaped orphan %s (context=%s) gone=%s", vmid, context, gone)
    return gone


@shared_task(bind=True)
def provision_student_instance(self, student_id: int):
    """CREATE (once) the student's PERSISTENT Kali box: reserve vmid (source 154) +
    ip -> FULL clone 154 INTO the reserved vmid onto lab2-vm -> size RAM/cores ->
    apply leased IP via cloud-init -> start -> confirm IP in guest -> reachable ->
    record RUNNING. Idempotent: if the student already has a non-torn-down box this
    is a NO-OP (never a re-clone). A failure runs the error-path teardown (force
    stop -> destroy -> release reservation + lease) leaving ZERO residue and sets
    status=error, so the slot frees and a retry starts clean."""
    result: dict = {"student_id": student_id, "steps": {}}
    steps = result["steps"]
    _assert_persistent_mode()

    try:
        sp = StudentProfile.objects.select_related("user").get(pk=student_id)
    except StudentProfile.DoesNotExist:
        result["error"] = f"StudentProfile {student_id} not found"
        result["verdict"] = "FAILED"
        return result

    # --- IDEMPOTENCY: CREATE ONCE — an existing box is returned, never recloned --
    existing = _existing_student_box(sp)
    if existing is not None:
        vm = existing.vms.first()
        result["labinstance_id"] = existing.pk
        result["vmid"] = getattr(vm, "vmid", None)
        result["ip"] = str(vm.ip.ip) if (vm and vm.ip_id) else None
        result["idempotent_noop"] = True
        result["verdict"] = "SUCCESS"
        return result

    idx = assign_student_index(sp)
    result["student_index"] = idx

    client = ProxmoxClient()
    result["tls_verify"] = client.verify

    # --- RUNTIME NEGATIVE SCOPE TEST FIRST (token must be refused on 109) --------
    neg_code, _ = client.raw_get_status(109)
    steps["negative_test_109"] = {"http": neg_code, "expected": 403,
                                  "pass": neg_code == 403}
    if neg_code != 403:
        write_audit(None, "provision.scope.abort", target_type="qemu",
                    target_id=109, http=neg_code, student_id=student_id,
                    reason="portal token NOT refused on NEVER_TOUCH 109")
        result["aborted"] = f"runtime negative test returned {neg_code}, expected 403"
        result["verdict"] = "FAILED"
        return result

    # --- capacity guard: STUDENT cap + count of non-torn-down student boxes ------
    ok, reason = capacity_ok(client=client, cap=_student_max_concurrent(),
                             active=student_box_count())
    steps["capacity"] = {"ok": ok, "reason": reason,
                         "cap": _student_max_concurrent()}
    if not ok:
        write_audit(None, "provision.rejected", target_type="StudentProfile",
                    target_id=sp.pk, reason=reason)
        result["error"] = f"capacity: {reason}"
        result["verdict"] = "REJECTED"
        return result

    _course, _module, exercise, template = _student_anchor()

    # --- create the LabInstance (PENDING) — the partial-unique constraint on
    #     owner_student makes a concurrent double-provision fail at the DB --------
    try:
        with transaction.atomic():
            lab = LabInstance.objects.create(
                owner_student=sp, lab_exercise=exercise, lab_template=template,
                status=LabInstance.Status.PENDING,
                provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT)
    except IntegrityError:
        existing = _existing_student_box(sp)
        result["labinstance_id"] = getattr(existing, "pk", None)
        result["idempotent_noop"] = True
        result["verdict"] = "SUCCESS"
        return result
    result["labinstance_id"] = lab.pk
    write_audit(None, "provision.request", target_type="LabInstance",
                target_id=lab.pk, student_id=sp.pk, mode=lab.provisioning_mode)

    vm = None
    lease = None
    vmid = None
    leased_ip = None
    name = None
    clone_done = False
    src_template = _student_source_template()
    storage = getattr(settings, "STUDENT_CLONE_STORAGE", "lab2-vm")
    try:
        # --- RESERVE vmid + ip, then clone, with BOUNDED SELF-HEAL over an
        #     ORPHAN blocking the chosen vmid (DEFECT 2 / the 9006 wedge). The
        #     allocator excludes VMIDs it can SEE live, but a stale orphan config
        #     the qemu list didn't surface can still occupy the reserved vmid and
        #     make the clone fail "config file already exists" forever. So: reap
        #     the orphan (guarded, audited) and retry the next free vmid. -------
        for attempt in range(1, CLONE_SELF_HEAL_ATTEMPTS + 1):
            steps.setdefault("clone_attempts", []).append(attempt)
            # RESERVE vmid (source 154) + ip
            vm = allocate_and_reserve_vmid(lab, client=client, role=Role.ATTACKER,
                                           source_template=src_template)
            vmid = vm.vmid
            result["vmid"] = vmid
            steps["reserved_vmid"] = vmid
            lease = lease_ip()
            leased_ip = str(lease.ip)
            steps["reserved_ip"] = leased_ip
            name = f"s{idx:02d}-kali-{vmid}"
            with transaction.atomic():
                vm.ip = lease
                vm.hostname = name
                vm.save(update_fields=["ip", "hostname"])
                lease.vm_instance = vm
                lease.save(update_fields=["vm_instance"])
            write_audit(None, "provision.reserve", target_type="qemu", target_id=vmid,
                        labinstance_id=lab.pk, student_id=sp.pk, ip=leased_ip)

            # SELF-HEAL (pre-clone): if the reserved vmid is already present on
            # Proxmox (VM and/or lingering config) and is NOT held by ANOTHER
            # active reservation, it is an orphan -> reap it before cloning.
            pre = client.get_status(vmid)
            pre_cfg = client.get_config(vmid)
            if (pre.get("exists") or pre_cfg.get("exists")) and \
                    vmid not in _active_reservation_vmids_excluding(vm):
                steps.setdefault("self_heal", []).append(
                    {"vmid": vmid, "phase": "pre-clone", "attempt": attempt})
                if not _reap_orphan_vmid(client, vmid, labinstance_id=lab.pk,
                                         context="pre-clone orphan"):
                    # couldn't clear it -> drop this reservation, try the next vmid
                    release_reservation(vm)
                    vm = lease = vmid = None
                    continue

            # FULL clone 154 -> reserved vmid onto lab2-vm
            write_audit(None, "provision.clone.start", target_type="qemu",
                        target_id=vmid, source=src_template, labinstance_id=lab.pk,
                        student_id=sp.pk, name=name, storage=storage)
            try:
                clone_upid = client.clone(src_template, vmid, name, full=True,
                                          pool=client.pool, storage=storage)
                steps["clone_upid"] = clone_upid
                cstat = client.wait_task(clone_upid, timeout=CAP_CLONE)
                steps["clone_task"] = {"status": cstat.get("status"),
                                       "exitstatus": cstat.get("exitstatus")}
                clone_done = True
                break
            except ProxmoxAPIError as cexc:
                # Clone failed. On the LAST attempt re-raise into the error-path
                # teardown (which reaps the reserved vmid). Otherwise self-heal any
                # orphan now occupying the vmid and retry the NEXT free vmid.
                steps.setdefault("clone_failures", []).append(
                    {"vmid": vmid, "attempt": attempt, "error": str(cexc)[:200]})
                write_audit(None, "provision.self_heal.retry", target_type="qemu",
                            target_id=vmid, labinstance_id=lab.pk, attempt=attempt,
                            error=str(cexc)[:200])
                logger.warning("clone attempt %s for vmid %s failed (%s); self-heal + retry",
                               attempt, vmid, cexc)
                if attempt >= CLONE_SELF_HEAL_ATTEMPTS:
                    raise
                if vmid not in _active_reservation_vmids_excluding(vm):
                    _reap_orphan_vmid(client, vmid, labinstance_id=lab.pk,
                                      context="post-clone-failure orphan")
                release_reservation(vm)
                vm = lease = vmid = None
                continue

        if not clone_done:
            raise ProxmoxAPIError(
                f"clone did not succeed within {CLONE_SELF_HEAL_ATTEMPTS} "
                "self-heal attempts")

        # --- SIZE the box (RAM/cores) on the stopped clone -------------------
        mem = int(getattr(settings, "STUDENT_RAM_MB", 4096))
        cores = int(getattr(settings, "STUDENT_CORES", 2))
        steps["set_resources"] = client.set_resources(vmid, memory=mem, cores=cores)
        write_audit(None, "provision.resize", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk, memory=mem, cores=cores)

        # --- APPLY the leased IP via cloud-init BEFORE start -----------------
        gw = getattr(settings, "PROVISION_IP_GATEWAY", "192.168.100.1")
        cidr = int(getattr(settings, "PROVISION_IP_CIDR", 24))
        steps["set_ipconfig"] = client.set_ipconfig(vmid, leased_ip, gw=gw, cidr=cidr)
        write_audit(None, "provision.ip_set", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk, ipconfig=steps["set_ipconfig"]["ipconfig"])

        # --- START -> confirm genuinely RUNNING (bounded) -------------------
        write_audit(None, "provision.start", target_type="qemu",
                    target_id=vmid, labinstance_id=lab.pk)
        start_upid = client.start(vmid)
        steps["start_upid"] = start_upid
        run_wait = client.wait_status(vmid, "running", timeout=CAP_START_RUNNING)
        steps["start_wait"] = run_wait
        if not run_wait.get("reached"):
            raise ProxmoxAPIError(
                f"start cap breached: {vmid} not 'running' within "
                f"{CAP_START_RUNNING}s (last={run_wait.get('status')!r})")

        # --- APPLY-CONFIRM: leased IP present on a guest interface -----------
        apply_start = time.monotonic()
        apply_deadline = apply_start + CAP_IP_APPLIED
        ip_in_guest = False
        last_ifaces = None
        while time.monotonic() < apply_deadline:
            ifaces = client.agent_get_interfaces(vmid)
            last_ifaces = ifaces
            if ifaces.get("ok") and leased_ip in ifaces.get("ips", []):
                ip_in_guest = True
                break
            time.sleep(3)
        steps["apply_confirm"] = {
            "ip_in_guest": ip_in_guest,
            "agent_ips": (last_ifaces or {}).get("ips", []),
            "waited_s": round(time.monotonic() - apply_start, 1)}
        if not ip_in_guest:
            raise ProxmoxAPIError(
                f"IP-apply not confirmed: leased {leased_ip} not present on a "
                f"guest interface within {CAP_IP_APPLIED}s "
                f"(agent said {(last_ifaces or {}).get('ips')})")
        write_audit(None, "provision.ip_applied", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk, ip=leased_ip,
                    waited_s=steps["apply_confirm"]["waited_s"])

        # --- REACHABILITY: worker TCP-connect to leased_ip:22 ---------------
        reach = _tcp_reachable(leased_ip, 22)
        steps["reachable"] = reach
        write_audit(None, "provision.reachable", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk, ip=leased_ip,
                    reachable=reach.get("reachable"), port=22,
                    waited_s=reach.get("waited_s"))

        # --- record RUNNING + ip_applied ------------------------------------
        with transaction.atomic():
            vm.proxmox_status = "running"
            vm.ip_applied = True
            vm.save(update_fields=["proxmox_status", "ip_applied"])
            lab.status = LabInstance.Status.RUNNING
            lab.save(update_fields=["status"])
        steps["vm_instance_id"] = vm.pk
        result["ip"] = leased_ip
        result["ip_applied"] = True
        result["reachable"] = reach.get("reachable")
        result["name"] = name
        write_audit(None, "provision.ok", target_type="LabInstance", target_id=lab.pk,
                    student_id=sp.pk, vmid=vmid, ip=leased_ip, source=src_template,
                    ip_applied=True, reachable=reach.get("reachable"))
        result["verdict"] = "SUCCESS"
        return result

    except Exception as exc:
        logger.exception("provision_student_instance failed; entering error-path teardown")
        result["error"] = f"{type(exc).__name__}: {exc}"

    # --- ERROR PATH: teardown -> ZERO residue, status=error (frees the slot) -----
    teardown: dict = {}
    if vmid is not None:
        if not clone_done and steps.get("clone_upid"):
            try:
                client.wait_task(steps["clone_upid"], timeout=CAP_CLONE)
            except Exception:  # noqa: BLE001 — best-effort; destroy handles the rest
                pass
        try:
            still = client.get_status(vmid)
            if still.get("exists"):
                cur = (still.get("data") or {}).get("status")
                if cur == "running":
                    logger.warning("error-path: %s still running -> force stop", vmid)
                    teardown["force_stop_upid"] = client.stop(vmid)
                    teardown["force_stop_wait"] = client.wait_status(
                        vmid, "stopped", timeout=CAP_FORCE_STOP)
                write_audit(None, "provision.destroy.start", target_type="qemu",
                            target_id=vmid, labinstance_id=lab.pk)
                du = client.destroy(vmid, purge=True)
                teardown["destroy_upid"] = du
                dstat = client.wait_task(du, timeout=CAP_TASK)
                teardown["destroy_task"] = {"status": dstat.get("status"),
                                            "exitstatus": dstat.get("exitstatus")}
            after = client.get_status(vmid)
            teardown["gone"] = not after.get("exists")
        except Exception as texc:
            teardown["destroy_error"] = f"{type(texc).__name__}: {texc}"
            logger.exception("error-path destroy failed")

    try:
        release_reservation(vm)
        lab.status = LabInstance.Status.ERROR
        lab.save(update_fields=["status"])
        teardown["db_cleaned"] = True
    except Exception as texc:
        teardown["db_error"] = f"{type(texc).__name__}: {texc}"
        logger.exception("error-path db cleanup failed")

    result["teardown"] = teardown
    write_audit(None, "provision.error", target_type="LabInstance", target_id=lab.pk,
                student_id=sp.pk, error=result.get("error"), gone=teardown.get("gone"))
    result["verdict"] = "FAILED"
    return result


@shared_task(bind=True)
def start_student_instance(self, labinstance_id: int):
    """START-ON-LOGIN: start the student's PERSISTENT box if it is stopped.
    Idempotent (already-running is a no-op) and NEVER a re-clone — it only powers
    on the existing VM."""
    result: dict = {"labinstance_id": labinstance_id, "steps": {}}
    steps = result["steps"]
    try:
        lab = LabInstance.objects.get(pk=labinstance_id)
    except LabInstance.DoesNotExist:
        result["error"] = f"LabInstance {labinstance_id} not found"
        result["verdict"] = "FAILED"
        return result

    vm = lab.vms.exclude(vmid__isnull=True).order_by("vmid").first()
    if vm is None or vm.vmid is None:
        result["error"] = "no VM with a vmid on this LabInstance"
        result["verdict"] = "FAILED"
        return result
    vmid = vm.vmid
    result["vmid"] = vmid

    client = ProxmoxClient()
    result["tls_verify"] = client.verify
    st = client.get_status(vmid)
    cur = (st.get("data") or {}).get("status")
    steps["current_status"] = cur
    if not st.get("exists"):
        result["error"] = f"vmid {vmid} does not exist on the node"
        result["verdict"] = "FAILED"
        return result

    if cur == "running":
        # already up — reconcile DB and return (idempotent, NOT a re-clone)
        if lab.status != LabInstance.Status.RUNNING:
            vm.proxmox_status = "running"
            vm.save(update_fields=["proxmox_status"])
            lab.status = LabInstance.Status.RUNNING
            lab.save(update_fields=["status"])
        result["idempotent_noop"] = True
        result["verdict"] = "SUCCESS"
        return result

    write_audit(None, "student.start", target_type="qemu", target_id=vmid,
                labinstance_id=lab.pk)
    steps["start_upid"] = client.start(vmid)
    run_wait = client.wait_status(vmid, "running", timeout=CAP_START_RUNNING)
    steps["start_wait"] = run_wait
    if not run_wait.get("reached"):
        write_audit(None, "student.start.timeout", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk)
        result["error"] = (f"start cap breached: {vmid} not 'running' within "
                           f"{CAP_START_RUNNING}s (last={run_wait.get('status')!r})")
        result["verdict"] = "FAILED"
        return result

    with transaction.atomic():
        vm.proxmox_status = "running"
        vm.save(update_fields=["proxmox_status"])
        lab.status = LabInstance.Status.RUNNING
        lab.save(update_fields=["status"])
    ip = str(vm.ip.ip) if vm.ip_id else None
    if ip:
        steps["reachable"] = _tcp_reachable(ip, 22)
        result["reachable"] = steps["reachable"].get("reachable")
    result["ip"] = ip
    write_audit(None, "student.start.ok", target_type="qemu", target_id=vmid,
                labinstance_id=lab.pk, waited_s=run_wait.get("waited_s"))
    result["verdict"] = "SUCCESS"
    return result


@shared_task(bind=True)
def stop_student_instance(self, labinstance_id: int):
    """STOP (explicit only): power OFF the student's PERSISTENT box (graceful ->
    force fallback). The VM, its vmid, its lease and disk are KEPT — the box
    persists and can be started again. Sets status=stopped."""
    result: dict = {"labinstance_id": labinstance_id, "steps": {}}
    steps = result["steps"]
    try:
        lab = LabInstance.objects.get(pk=labinstance_id)
    except LabInstance.DoesNotExist:
        result["error"] = f"LabInstance {labinstance_id} not found"
        result["verdict"] = "FAILED"
        return result

    vm = lab.vms.exclude(vmid__isnull=True).order_by("vmid").first()
    if vm is None or vm.vmid is None:
        result["error"] = "no VM with a vmid on this LabInstance"
        result["verdict"] = "FAILED"
        return result
    vmid = vm.vmid
    result["vmid"] = vmid

    client = ProxmoxClient()
    result["tls_verify"] = client.verify
    st = client.get_status(vmid)
    cur = (st.get("data") or {}).get("status")
    steps["current_status"] = cur
    if st.get("exists") and cur == "running":
        write_audit(None, "student.stop", target_type="qemu", target_id=vmid,
                    labinstance_id=lab.pk)
        client.shutdown(vmid)
        grace = client.wait_status(vmid, "stopped", timeout=CAP_GRACE_STOP)
        steps["graceful_stop_wait"] = grace
        if grace.get("reached"):
            steps["stop_path"] = "graceful"
        else:
            logger.warning("student.stop: %s graceful cap breached -> force", vmid)
            client.stop(vmid)
            steps["forced_stop_wait"] = client.wait_status(
                vmid, "stopped", timeout=CAP_FORCE_STOP)
            steps["stop_path"] = "forced"
            if not steps["forced_stop_wait"].get("reached"):
                result["error"] = f"force stop cap breached: {vmid} not stopped"
                result["verdict"] = "FAILED"
                return result

    # KEEP the VM/vmid/lease — persistent. Only flip the recorded status.
    with transaction.atomic():
        vm.proxmox_status = "stopped"
        vm.save(update_fields=["proxmox_status"])
        lab.status = LabInstance.Status.STOPPED
        lab.save(update_fields=["status"])
    write_audit(None, "student.stop.ok", target_type="qemu", target_id=vmid,
                labinstance_id=lab.pk, stop_path=steps.get("stop_path", "already-stopped"))
    result["verdict"] = "SUCCESS"
    return result


@shared_task(bind=True)
def deprovision_student_instance(self, labinstance_id: int):
    """DESTROY (explicit teardown only): graceful -> force stop -> destroy -> free
    lease + reservation -> status=destroyed. This is the ONLY path that removes a
    persistent box. Idempotent / retry-safe."""
    result: dict = {"labinstance_id": labinstance_id, "vms": []}
    try:
        lab = LabInstance.objects.get(pk=labinstance_id)
    except LabInstance.DoesNotExist:
        result["error"] = f"LabInstance {labinstance_id} not found"
        result["verdict"] = "FAILED"
        return result

    client = ProxmoxClient()
    result["tls_verify"] = client.verify
    write_audit(None, "student.deprovision.start", target_type="LabInstance",
                target_id=lab.pk)

    all_gone = True
    for vm in list(lab.vms.all()):
        v: dict = {"vm_instance_id": vm.pk, "vmid": vm.vmid}
        try:
            if vm.vmid is not None:
                st = client.get_status(vm.vmid)
                if st.get("exists"):
                    cur = (st.get("data") or {}).get("status")
                    if cur == "running":
                        client.shutdown(vm.vmid)
                        grace = client.wait_status(vm.vmid, "stopped",
                                                   timeout=CAP_GRACE_STOP)
                        v["graceful_stop_wait"] = grace
                        if not grace.get("reached"):
                            client.stop(vm.vmid)
                            v["forced_stop_wait"] = client.wait_status(
                                vm.vmid, "stopped", timeout=CAP_FORCE_STOP)
                            v["stop_path"] = "forced"
                        else:
                            v["stop_path"] = "graceful"
                    write_audit(None, "student.deprovision.destroy.start",
                                target_type="qemu", target_id=vm.vmid,
                                labinstance_id=lab.pk)
                    du = client.destroy(vm.vmid, purge=True)
                    v["destroy_upid"] = du
                    dstat = client.wait_task(du, timeout=CAP_TASK)
                    v["destroy_task"] = {"status": dstat.get("status"),
                                         "exitstatus": dstat.get("exitstatus")}
                after = client.get_status(vm.vmid)
                v["gone"] = not after.get("exists")
                all_gone = all_gone and v["gone"]

            with transaction.atomic():
                lease_pks = list(
                    IPLease.objects.filter(vm_instance=vm).values_list("pk", flat=True))
                for pk in lease_pks:
                    release_lease(pk)
                vm.delete()
            v["released_leases"] = lease_pks
        except Exception as exc:
            v["error"] = f"{type(exc).__name__}: {exc}"
            all_gone = False
            logger.exception("student deprovision of vmid %s failed", vm.vmid)
        result["vms"].append(v)

    if all_gone:
        lab.status = LabInstance.Status.DESTROYED
        lab.save(update_fields=["status"])
        write_audit(None, "student.deprovision.ok", target_type="LabInstance",
                    target_id=lab.pk)
        result["verdict"] = "SUCCESS"
    else:
        write_audit(None, "student.deprovision.partial", target_type="LabInstance",
                    target_id=lab.pk)
        result["verdict"] = "PARTIAL"
    result["status"] = lab.status
    return result


@shared_task(bind=True)
def stop_idle_students(self):
    """STUB (gated OFF): idle auto-stop for the persistent pilot is DELIBERATELY
    disabled — boxes stay up until an explicit stop. Enabling would require
    operator sign-off AND an implementation; it is not built here."""
    if not getattr(settings, "STUDENT_IDLE_AUTOSTOP_ENABLED", False):
        return {"enabled": False, "noop": True,
                "reason": "idle auto-stop disabled for the persistent pilot"}
    raise NotImplementedError(
        "idle auto-stop is enabled by config but not implemented for the pilot "
        "(persistent boxes). Implement + get operator sign-off before enabling.")
