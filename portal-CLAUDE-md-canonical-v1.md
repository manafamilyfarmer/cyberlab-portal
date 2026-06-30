# CyberLab Portal — Build Guardrails for Claude Code

## What this is
A Django + DRF student-portal that controls the CyberLab as the students' front door.
You write/scaffold code here. You do NOT operate production VMs from this repo.

## Build order (do not skip ahead)
B1 = manual-assisted: accounts, curriculum, labs (records only), scheduling display,
submissions, dashboards, audit. NO Proxmox automation in B1.
Proxmox integration starts at B2 and runs ONLY inside Celery workers, never web requests.

## Hard rules
- Secrets (SECRET_KEY, DB creds, Proxmox token) come from env / Docker secrets.
  Never hardcode, never commit. .env is gitignored; update .env.example instead.
- The portal DB is `portaldb` on VM112, reachable ONLY from VM114, SEPARATE from the
  lab's `cyberlabdb`. Never point the portal at cyberlabdb.
- Proxmox access (B2+) uses the portal's OWN scoped token (pool cyberlab-agent), never
  root, never VMs 106/109/110. Validate every VMID against the pool AND the reserved
  student range (9000+) before any call.
- Student clones: VMID 9000+, IP 192.168.100.150-249 via IPLease; release the lease on
  teardown. Never use infra IPs (.10-.100).
- Enforce the global capacity ceiling before queueing any start/clone.
- Enforce RBAC in DRF permissions, not just templates. Students see/act on their OWN
  assignments/instances only.
- Treat every uploaded submission as hostile: allowlist type, cap size, store outside
  webroot, never execute, record sha256.
- Every admin/instructor write and every provisioning action writes an AuditLog row.
- Provisioning jobs are idempotent (idempotency_key) and retry-safe.
- Do not add public-internet exposure. Do not store real cloud credentials.
- Do NOT touch host networking / the Security Onion mirror script — mirroring of clones
  is handled host-side (pool-aware script), not from the portal.

## When unsure
State the plan before generating migrations or provisioning code. Ask before anything
that would touch a real VM, change network/firewall, or alter host-side scripts.

---
## Repo operational note (B1 Step 2 — environment-specific, not in §18)
Secrets are delivered to containers via a **read-only bind mount** of
`/opt/cyberlab-portal/secrets/` (the Docker daemon mounts as root; the files stay
`root:root 600`), sourced in `entrypoint.sh` which then drops to a non-root app user via
`gosu`. Consequence: `docker compose exec` **bypasses the entrypoint**, so any one-shot
`manage.py` command (migrate, createsuperuser, shell) MUST be run **through the entrypoint**,
e.g. `docker compose exec web /app/entrypoint.sh python manage.py migrate`. Running
`manage.py` directly will fall back to dev defaults and fail to reach `portaldb`.
