FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# gosu lets the entrypoint read root:600 secrets, then drop to a non-root user.
# openssh-client is the read-only WireGuard status channel to vpn01 (B4.5): the
# poller shells out to `ssh` with a fixed argv + pinned known_hosts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Non-root application user.
RUN groupadd --system app && useradd --system --gid app --home-dir /app app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Collect static into STATIC_ROOT (/app/staticfiles) at BUILD time so the image
# ships ready-to-serve assets and no boot does filesystem work (B6.1). This runs
# against config.settings.prod deliberately: that module selects the manifest
# storage backend, so staticfiles.json is generated here and baked in.
#
# collectstatic only IMPORTS settings — it opens no DB connection and reads no
# secret. The values below exist solely to satisfy that import (base.py reads
# them via os.environ[...] at module scope) and are throwaway placeholders, NOT
# secrets: they are scoped to this RUN layer and the real values still come from
# the read-only bind-mounted env_files at runtime.
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    DJANGO_SECRET_KEY=build-time-collectstatic-placeholder \
    PORTAL_DB_NAME=build PORTAL_DB_USER=build \
    PORTAL_DB_PASSWORD=build PORTAL_DB_HOST=127.0.0.1 \
    python manage.py collectstatic --noinput --clear

RUN chmod +x /app/entrypoint.sh && chown -R app:app /app

EXPOSE 8000

# Entrypoint starts as root to load the bind-mounted secrets, then execs the
# CMD as the unprivileged "app" user.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-b", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
