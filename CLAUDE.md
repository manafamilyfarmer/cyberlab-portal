# cyberlab-portal — Guardrails for Claude Code (B0 §18)

> Source note: the canonical B0 §18 text was not available on the build host
> when this repo was scaffolded (B1 Step 2). This file captures the portal
> guardrails faithfully from the established security model; reconcile against
> the canonical B0 §18 when available.

## What this repo is
The SethuAI CyberLab training portal: Django + DRF (gunicorn), Celery
worker/beat, and redis, backed by a dedicated PostgreSQL database (`portaldb`
on VM112) reached over TLS. It is deployed as a container stack on VM114
(dockerlab01, 192.168.100.92), inside the lab — never exposed to the public
Internet.

## Secrets
- Secrets live ONLY on VM114 under `/opt/cyberlab-portal/secrets/` (root:root, 600):
  - `portaldb.env` — DB connection (provisioned in B1 Step 1; do not edit here).
  - `portal-app.env` — `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_SETTINGS_MODULE`.
- NEVER commit a secret. `.env` and `*.env` are gitignored; `.env.example`
  documents shape only (keys, no values).
- NEVER print secret values in logs, reports, or commit messages.
- Secret files must stay OUT of the Docker build context (see `.dockerignore`).

## Database
- The portal uses `portaldb` ONLY. NEVER point Django at `cyberlabdb`.
- The DB connection MUST use `sslmode=require` (TLS). It is external (VM112);
  there is no `db` service in compose.
- `portaldb` is reachable only from VM114 over TLS by design (pg_hba on VM112).

## Network
- `web` binds to `192.168.100.92:8000` only — never `0.0.0.0` publicly, no public IP.
- `redis` is internal to the compose network — NEVER publish a host port for it.
- Do not expose the portal or any lab VM to the public Internet.

## Change discipline
- Idempotent / re-runnable: update in place, do not duplicate repo or stack.
- Snapshot VM114 before bringing the stack up or making material changes.
- On any failed gate or verification, STOP and report — do not "fix forward".
- No business models / `makemigrations` for `apps/*` until the step that owns them.
  Built-in Django migrations (auth/contenttypes/sessions/admin) are the baseline.

## Out of scope for this repo's automation
- Do not touch VM106 (dc01), VM109 (wazuh01), VM110 (securityonion01).
- Do not modify host networking, host firewall, or the Security Onion mirror script.
