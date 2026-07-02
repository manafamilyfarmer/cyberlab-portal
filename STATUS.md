# CyberLab Portal — STATUS

Live "you are here" for the build. Updated as the last action of each step. The commit log is the authoritative ledger; this file is the human pointer.

Last updated: 2026-07-03 · HEAD: ff1ba1d

## Done
- B0 — portal design (v1.1) accepted.
- B1 — manual-assisted portal, all 8 steps COMPLETE and accepted.
- B2 Step 1 — clone primitive: clone 151->9000 -> record -> destroy -> lease release, zero residue, verified TLS.
- B2 Step 2 — power lifecycle: start -> running -> graceful/force stop -> destroy, bounded waits, zero residue.
- B2 Step 4 — shared-model provisioning bound to a batch: instructor/admin provision (web validates + enqueues, worker clones->starts->records->leases IP recorded-only) + deprovision to zero residue; RBAC (students read-only own-batch, instructors own-batch writes) + capacity guard. Verified, zero residue.
- B2 Step 4a — atomic reserve-then-clone allocator: VMInstance.vmid UNIQUE arbitrates VMID; IP claimed with select_for_update(skip_locked). Concurrency- + retry-safe. Proven: 10 parallel reservations all distinct; real parallel double-provision -> distinct vmids (9000/9001) + IPs, both running, no collision; retry = idempotent no-op. Zero residue.

## Learned
- Provisioning allocation must be ATOMIC (reserve-then-clone). Check-then-act ("lowest free number" then clone) races under concurrent/retried provisions — two tasks pick the same VMID/IP. The DB (unique constraint + row locks) must arbitrate.
- Reserving the VMID first also makes the error path reliably reap residue (it knows the exact vmid even on a clone timeout).
- Parallel full clones contend for storage I/O and run slower — bound the clone wait with headroom (CAP_CLONE=600s), not the single-clone cap.

## In progress / next
- Cloud-init template prep (host-side) — build a cloud-init-ready Ubuntu template to unblock B2.3.
- B2 Step 5 — per-student provisioning model (owner_student binding).

## Blocked / waiting
- B2 Step 3 — IP injection + reachability. Blocked until the cloud-init-ready template is verified.

## Carried items
- Wazuh forwarding of AuditLog + provision.* / submission.infected.
- portaldb scheduled backups -> backuprepo via Celery job.
- XFF trusted-proxy list (with the B2 access model).
- Operator: TOTP-enroll cyberadmin, then delete portal-admin.env.
- Import the real Track A catalog into curriculum.
- LabExercise -> LabTemplate FK (deferred from B1).
- SO mirror script pool-aware — before B3 (host-side).
- Migrate portaldb to a dedicated non-target DB host at B3.
