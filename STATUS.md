# CyberLab Portal — STATUS

Live "you are here" for the build. Updated as the last action of each step. The commit log is the authoritative ledger; this file is the human pointer.

Last updated: 2026-07-01 · HEAD: cfde456

## Done
- B0 — portal design (v1.1) accepted.
- B1 — manual-assisted portal, all 8 steps COMPLETE and accepted.
- B2 Step 1 — clone primitive: clone 151->9000 -> record -> destroy -> lease release, zero residue, verified TLS.
- B2 Step 2 — power lifecycle: start -> running -> graceful/force stop -> destroy, bounded waits, zero residue.

## In progress / next
- B2 Step 4 — shared-model provisioning bound to a batch (provision/deprovision + RBAC).
- Cloud-init template prep (host-side) — build a cloud-init-ready Ubuntu template to unblock B2.3.

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
