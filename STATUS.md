# CyberLab Portal — STATUS

Live "you are here" for the build. Updated as the last action of each step. The commit log is the authoritative ledger; this file is the human pointer.

Last updated: 2026-07-03 · HEAD: eaf691f

## Done
- B0 — portal design (v1.1) accepted.
- B1 — manual-assisted portal, all 8 steps COMPLETE and accepted.
- B2 Step 1 — clone primitive: clone 151->9000 -> record -> destroy -> lease release, zero residue, verified TLS.
- B2 Step 2 — power lifecycle: start -> running -> graceful/force stop -> destroy, bounded waits, zero residue.
- B2 Step 4 — shared-model provisioning bound to a batch: instructor/admin provision (web validates + enqueues, worker clones->starts->records->leases IP recorded-only) + deprovision to zero residue; RBAC (students read-only own-batch, instructors own-batch writes) + capacity guard. Verified, zero residue.
- B2 Step 4a — atomic reserve-then-clone allocator: VMInstance.vmid UNIQUE arbitrates VMID; IP claimed with select_for_update(skip_locked). Concurrency- + retry-safe. Proven: 10 parallel reservations all distinct; real parallel double-provision -> distinct vmids (9000/9001) + IPs, both running, no collision; retry = idempotent no-op. Zero residue.
- B2 Step 3 — cloud-init IP apply + reachability: provisioning source is now template 153 (cloud-init-ready Ubuntu 24.04, guest-agent, ACPI). Leased IP APPLIED via ipconfig0; confirmed inside the guest via the agent (ip_applied=True) and reachable from the worker (TCP:22) and the host. Deprovision stops GRACEFULLY (153 has ACPI), IP released + no longer reachable. First student-usable lab. Concurrency spot-check still distinct + both reachable. Zero residue.
- B2 orphan reaper — periodic Celery-beat sweep (every REAPER_INTERVAL=600s). Destroys a 9000-range VM ONLY under all four AND-conditions (9000-range + portal name-prefix + no active DB reservation + age>REAPER_GRACE=900s); cleans stale reservations + orphaned leases; audited (reaper.*); dry-run + idempotent. Proven: real orphan reaped + legit instance SKIPPED (reservation) + never-touch 106/109/110 guard-raised + non-prefix skipped. Zero residue. **B3 fan-out gate CLEARED.**
- Wazuh forwarding **COMPLETE** (closes B2 observability) — **Part 1 (JSON emit):** every write_audit() ALSO emits one compact JSON line to AUDIT_LOG_PATH (/var/cyberlab-portal-logs/audit.jsonl, dedicated volume, NOT web-served, 0640 app:app), IN ADDITION to the unchanged AuditLog DB row. 'cyberlab.audit' logger + RotatingFileHandler (50MiB x5, size-capped) + JSONL formatter; stable schema (@timestamp/event_type/category/actor/actor_role/target_type/target_id/source_ip/result/detail/host); recursive secret-scrub; log-and-continue. Proven: valid JSONL across auth/admin/reaper/provisioning + ok/error/info; both sinks aligned; scrub redacts password/token/key/db_password/nested private_key; non-fatal + recovery; real worker-written provision.*/deprovision.ok. **Part 2 (transport):** VM114 enrolled as a Wazuh agent (ID 005, Active) against the manager at 192.168.100.70, tailing audit.jsonl; end-to-end event confirmed in the stream. Zero residue.

## Learned
- Provisioning allocation must be ATOMIC (reserve-then-clone). Check-then-act ("lowest free number" then clone) races under concurrent/retried provisions — two tasks pick the same VMID/IP. The DB (unique constraint + row locks) must arbitrate.
- Reserving the VMID first also makes the error path reliably reap residue (it knows the exact vmid even on a clone timeout).
- Parallel full clones contend for storage I/O and run slower — bound the clone wait with headroom (CAP_CLONE=600s), not the single-clone cap.
- Wazuh stores ALERTS by default, not raw events — a rule match is what surfaces in alerts.json. To archive every ingested event (not just matches), enable logall_json on the manager. So a quiet alerts.json does NOT mean the agent isn't forwarding; confirm at the archive/ingestion layer.

## In progress / next
- B2 Step 5 — per-student provisioning model (owner_student binding).

## Blocked / waiting
- (none — B2.3 unblocked; cloud-init template 153 verified and in use.)

## Carried items
- SOP §8 should name template 153 (cloud-init) as the shared-provisioning source (152/Kali stays available).
- portaldb scheduled backups -> backuprepo via Celery job.
- XFF trusted-proxy list (with the B2 access model).
- Operator: TOTP-enroll cyberadmin, then delete portal-admin.env.
- Import the real Track A catalog into curriculum.
- LabExercise -> LabTemplate FK (deferred from B1).
- SO mirror script pool-aware — before B3 (host-side).
- Migrate portaldb to a dedicated non-target DB host at B3.
