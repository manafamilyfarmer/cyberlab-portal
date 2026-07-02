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

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.audit.services import write_audit
from apps.curriculum.models import Course, LabExercise, Module
from apps.labs.models import IPLease, LabInstance, LabTemplate, Role, VMInstance

from .pve import ProxmoxClient, ProxmoxAPIError

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
CAP_TASK = 300            # clone/destroy task poll cap


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
