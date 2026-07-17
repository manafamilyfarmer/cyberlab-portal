# cyberlab-portal

Training/lab portal for the SethuAI CyberLab. Django + DRF (gunicorn) with a
Celery worker/beat and redis, talking to a dedicated PostgreSQL database
(`portaldb` on VM112) over TLS.

## Architecture

| Service | Role | Notes |
|---------|------|-------|
| web     | Django + DRF via gunicorn | bound to `192.168.100.92:8000` (internal only) |
| worker  | Celery worker | broker/backend = redis |
| beat    | Celery beat scheduler | |
| redis   | broker/result backend | internal compose network, **no host port** |

The database is **external** (VM112, `portaldb`) — there is no `db` service.
The portal must never use the lab's `cyberlabdb`.

## Secrets

Secrets are **never** committed. They live on VM114 only, mode 600:

- `/opt/cyberlab-portal/secrets/portaldb.env` — DB host/port/name/user/password/sslmode
- `/opt/cyberlab-portal/secrets/portal-app.env` — `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_SETTINGS_MODULE`

`.env.example` documents the shape (keys only, no values).

## Deploy (VM114) — git pull, never a copy

`/opt/cyberlab-portal/app` on VM114 **is a git checkout** on branch `main`,
tracking `origin/main` through a read-only deploy key. Deploying = pulling the
commit you already pushed, so what runs in the lab is always a named commit you
can `git log`.

```sh
# on VM114, as the operator (the checkout is owned by cyberadmin):
cd /opt/cyberlab-portal/app
git pull                       # fast-forward to the pushed commit
sudo docker compose build web  # web holds `build: .`; worker/beat share the image
sudo docker compose up -d
```

Add `docker compose exec -T web /app/entrypoint.sh python manage.py migrate`
when the step ships a migration.

The image is **baked** — there is no app bind-mount — so `build web` is what
makes new code (and `collectstatic`'s fingerprinted assets) take effect.
`git pull` alone changes nothing that is running.

> **Why this is written down.** VM114 used to hold a *detached copy* of the tree
> that was rsynced into place. Nothing tied it to a commit, so a partial sync
> left stale assets serving next to fresh templates and the drift was invisible —
> the checkout has no "which commit am I?" answer when it isn't a checkout. If
> you ever find yourself about to `rsync`/`scp` source onto VM114, that is the
> incident restarting: push to `origin` and pull instead.

## Health

- `GET /healthz/` → `200 ok` (no DB)
- `GET /readyz/`  → `200` if `SELECT 1` over the TLS DB path succeeds, else `503`

## Settings

`config/settings/{base,dev,prod}.py`. The container runs `config.settings.prod`
(set via `portal-app.env`). DB connection requires `sslmode=require`.

No business models exist yet — `apps/{accounts,curriculum,labs,assessments,provisioning,audit}`
are empty installable apps. Models arrive in later build steps.
