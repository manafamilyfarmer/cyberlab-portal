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

## Deploy (VM114)

```sh
cd /opt/cyberlab-portal/app
docker compose build
docker compose up -d
docker compose exec -T web python manage.py migrate
```

## Health

- `GET /healthz/` → `200 ok` (no DB)
- `GET /readyz/`  → `200` if `SELECT 1` over the TLS DB path succeeds, else `503`

## Settings

`config/settings/{base,dev,prod}.py`. The container runs `config.settings.prod`
(set via `portal-app.env`). DB connection requires `sslmode=require`.

No business models exist yet — `apps/{accounts,curriculum,labs,assessments,provisioning,audit}`
are empty installable apps. Models arrive in later build steps.
