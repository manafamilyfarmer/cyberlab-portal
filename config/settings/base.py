"""Base settings shared by dev and prod.

Secrets and the database connection are read from the environment. On VM114
these come from the env_files /opt/cyberlab-portal/secrets/{portaldb.env,
portal-app.env}. Never hard-code secret values here.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SECURITY: required from the environment; no literal fallback (see dev.py for
# a local-only default seeded via os.environ.setdefault before this imports).
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

ALLOWED_HOSTS = ["192.168.100.92", "127.0.0.1", "localhost", "testserver"]

# Custom user model (B1 Step 3).
AUTH_USER_MODEL = "accounts.User"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "axes",
    # local apps
    "apps.accounts",
    "apps.audit",
    "apps.curriculum",
    "apps.labs",
    "apps.scheduling",
    "apps.assessments",
    "apps.provisioning",
    "apps.dashboard",
]

# Axes backend FIRST so lockouts are enforced during authenticate().
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves STATIC_ROOT directly from gunicorn (B6.1) — no nginx in
    # front of the stack. It must sit immediately after SecurityMiddleware so
    # static responses still get the security headers but skip session/auth work.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # django_otp after authentication
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # django-axes must be LAST
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-level templates (B6.2): base.html + the pages that are not
        # owned by a single app. App template dirs still resolve via APP_DIRS.
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database — portaldb on VM112 over TLS. NEVER cyberlabdb.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["PORTAL_DB_NAME"],
        "USER": os.environ["PORTAL_DB_USER"],
        "PASSWORD": os.environ["PORTAL_DB_PASSWORD"],
        "HOST": os.environ["PORTAL_DB_HOST"],
        "PORT": os.environ.get("PORTAL_DB_PORT", "5432"),
        "OPTIONS": {"sslmode": os.environ.get("PORTAL_DB_SSLMODE", "require")},
    }
}

# Session frontend entry point. @login_required and LoginRequiredMixin redirect
# here; the view itself already lives at /accounts/login/ (B1).
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static files (B6.1: served by WhiteNoise, no nginx) -------------------------
# STATIC_ROOT is the collectstatic OUTPUT dir (/app/staticfiles in the image) and
# is git-ignored — it is generated at image build time, never committed.
# STATICFILES_DIRS is the project-level SOURCE dir for custom (non-app) assets.
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Spelled out (rather than left to the Django default) so prod.py can override
# just the "staticfiles" backend while inheriting "default" — see prod.py.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Sessions (B0 §13 auth hardening) ---
SESSION_COOKIE_AGE = 3600
SESSION_SAVE_EVERY_REQUEST = True          # sliding idle timeout
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
# httponly + samesite always on; the secure flag is env-gated in prod.py.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

# --- Login lockout (django-axes) ---
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # hours
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True

# --- Django REST Framework ---
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# Celery (redis on the internal compose network)
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/0")

# B2 provisioning: max concurrently-held lab instances (pending+running). The
# capacity guard rejects new provisions past this cap (honored in the worker and
# in the web DB pre-check).
PROVISION_MAX_CONCURRENT = int(os.environ.get("PROVISION_MAX_CONCURRENT", "10"))

# Shared-model source template. 153 = cloud-init-ready Ubuntu 24.04 (qemu-guest-
# agent baked in, ACPI works) — the leased IP is APPLIED via cloud-init (B2.3).
# Must be in the pve CLONE_SOURCE_ALLOWLIST. 152 (Kali) stays available for later.
PROVISION_SOURCE_TEMPLATE = int(os.environ.get("PROVISION_SOURCE_TEMPLATE", "153"))
# Lab IP network (for cloud-init ipconfig0). Pool is 192.168.100.150-249 /24.
PROVISION_IP_GATEWAY = os.environ.get("PROVISION_IP_GATEWAY", "192.168.100.1")
PROVISION_IP_CIDR = int(os.environ.get("PROVISION_IP_CIDR", "24"))

# --- SEAM 1 (SOP §11): the Proxmox NODE name is a SETTING, never hardcoded in
# pve.py. Every node reference in the Proxmox client reads this. Change this ONE
# value to move provisioning to another node without touching pve.py URLs/paths.
PROVISION_TARGET_NODE = os.environ.get("PROVISION_TARGET_NODE", "proxmox")

# --- SEAM 2 (SOP §11): lifecycle mode.
#   "persistent" (pilot DEFAULT) = one PERSISTENT box per student: created once,
#       kept for the pilot, started-on-login, stopped/destroyed on explicit action.
#   "ephemeral"  (future scale)  = a NOT-IMPLEMENTED stub that raises
#       NotImplementedError. The seam exists so the future path has a home; the
#       code path does NOT exist yet and must not be built here.
LIFECYCLE_MODE = os.environ.get("LIFECYCLE_MODE", "persistent")

# --- B3 Step 1: per-student PERSISTENT Kali box ---------------------------------
# Source template 154 (Kali), FULL clone onto storage lab2-vm. Exactly ONE active
# box per student, created ONCE (instructor/admin trigger, NOT at login) and kept
# for the pilot. Must be in the pve CLONE_SOURCE_ALLOWLIST.
STUDENT_SOURCE_TEMPLATE = int(os.environ.get("STUDENT_SOURCE_TEMPLATE", "154"))
STUDENT_CLONE_STORAGE = os.environ.get("STUDENT_CLONE_STORAGE", "lab2-vm")
STUDENT_RAM_MB = int(os.environ.get("STUDENT_RAM_MB", "4096"))
STUDENT_CORES = int(os.environ.get("STUDENT_CORES", "2"))
# Concurrency cap for per-student boxes (distinct from the shared cap above).
STUDENT_MAX_CONCURRENT = int(os.environ.get("STUDENT_MAX_CONCURRENT", "12"))
# Idle auto-stop is DELIBERATELY OFF for the pilot (boxes are persistent). The
# stub task exists but is gated on this flag; never enable without operator sign-off.
STUDENT_IDLE_AUTOSTOP_ENABLED = (
    os.environ.get("STUDENT_IDLE_AUTOSTOP_ENABLED", "0")
    not in ("0", "false", "no", "off")
)

# Orphan reaper (periodic Celery-beat sweep). Destroys a 9000-range VM ONLY when
# it matches the portal name prefix, has NO active DB reservation, and is older
# than REAPER_GRACE. Also cleans stale reservations + orphaned leases.
REAPER_ENABLED = os.environ.get("REAPER_ENABLED", "1") not in ("0", "false", "no", "off")
REAPER_GRACE = int(os.environ.get("REAPER_GRACE", "900"))      # 15 min
REAPER_INTERVAL = int(os.environ.get("REAPER_INTERVAL", "600"))  # 10 min
REAPER_NAME_PREFIX = os.environ.get("REAPER_NAME_PREFIX", "b2-")
CELERY_BEAT_SCHEDULE = {
    "reap-orphans": {
        "task": "apps.provisioning.reaper.reap_orphans",
        "schedule": float(REAPER_INTERVAL),
    },
    # B4.5: read-only WireGuard status poll of vpn01 -> per-peer cache. Interval
    # read from env directly (WG_STATUS_* settings are defined lower in this file).
    "poll-wireguard-status": {
        "task": "apps.provisioning.tasks.poll_wireguard_status",
        "schedule": float(os.environ.get("WG_STATUS_POLL_INTERVAL", "45")),
    },
}

# --- Submissions (hostile-upload pipeline, B0 §13/§20) ---
# Dedicated volume OUTSIDE the web root / any URL-served path. Never under /app.
SUBMISSIONS_DIR = os.environ.get("SUBMISSIONS_DIR", "/var/cyberlab-submissions")
SUBMISSION_MAX_BYTES = int(os.environ.get("SUBMISSION_MAX_BYTES", str(10 * 1024 * 1024)))
SUBMISSION_ALLOWED_TYPES = [
    "image/png",
    "image/jpeg",
    "image/gif",
    "application/pdf",
    "text/plain",
    "application/zip",
    "application/x-zip-compressed",
    "application/gzip",
    "application/x-tar",
]
# ClamAV clamd (internal compose network; INSTREAM scan, no shared volume).
CLAMAV_HOST = os.environ.get("CLAMAV_HOST", "clamav")
CLAMAV_PORT = int(os.environ.get("CLAMAV_PORT", "3310"))

# --- Audit JSON stream (Wazuh part 1: emit side) ---
# Every write_audit() ALSO emits one compact JSON line here, IN ADDITION to the
# AuditLog DB row. This JSONL file is the SIEM ingestion source (a Wazuh agent /
# syslog / API tails it). It lives on a dedicated volume OUTSIDE the app tree and
# is NEVER web-served. The entrypoint creates the dir (app-user-owned, 0750) and
# the file (0640, not world-readable) before dropping privileges. A logging
# failure here must NEVER break the audited action (write_audit is log-and-continue).
AUDIT_LOG_DIR = os.environ.get("AUDIT_LOG_DIR", "/var/cyberlab-portal-logs")
AUDIT_LOG_PATH = os.environ.get(
    "AUDIT_LOG_PATH", os.path.join(AUDIT_LOG_DIR, "audit.jsonl")
)
# Size-capped rotation so the stream can never fill the disk. Rotation across the
# gunicorn + celery worker/beat processes is best-effort (append-mode, size cap);
# see apps.audit.emit for the justification.
AUDIT_LOG_MAX_BYTES = int(os.environ.get("AUDIT_LOG_MAX_BYTES", str(50 * 1024 * 1024)))
AUDIT_LOG_BACKUPS = int(os.environ.get("AUDIT_LOG_BACKUPS", "5"))

# --- WireGuard config distribution (B4.4) ---
# Pre-generated per-student .conf files + manifest.tsv are bind-mounted READ-ONLY
# at /run/portal-secrets/wg (600 root:root). The non-root "app" user the service
# runs as CANNOT read those, so the entrypoint stages app-readable 0400 copies
# into container tmpfs at WG_SECRETS_DIR (mirrors the portal-pve.env staging).
# The app reads configs from WG_SECRETS_DIR by the DB pointer (config_secret_ref)
# and streams the bytes at download time; the bytes are NEVER logged and NEVER
# stored in the DB. The source bind mount stays 600 root:root read-only.
WG_SECRETS_DIR = os.environ.get("WG_SECRETS_DIR", "/run/portal-app-secrets/wg")
# Source (read-only bind mount) the entrypoint stages FROM. Not read by the app.
WG_SOURCE_DIR = os.environ.get("WG_SOURCE_DIR", "/run/portal-secrets/wg")

# --- WireGuard LIVE status (B4.5): read-only poll of vpn01 -----------------------
# The portal polls a locked-down forced-command SSH channel on vpn01 and caches
# per-peer connection state. The portal is READ-ONLY toward vpn01 (no writes).
# The SSH PRIVATE key is a secret: bind-mounted read-only, staged app-readable
# 0400 by entrypoint.sh (WG_STATUS_KEY). The pinned known_hosts (host PUBLIC key,
# non-secret) is baked into the image; the shipped path uses
# StrictHostKeyChecking=yes — never accept-new.
WG_STATUS_SOURCE_KEY = os.environ.get(
    "WG_STATUS_SOURCE_KEY", "/run/portal-secrets/wg-status/id_wgstatus"
)
WG_STATUS_KEY = os.environ.get(
    "WG_STATUS_KEY", "/run/portal-app-secrets/wg-status/id_wgstatus"
)
WG_STATUS_HOST = os.environ.get("WG_STATUS_HOST", "192.168.100.7")
WG_STATUS_USER = os.environ.get("WG_STATUS_USER", "wgstatus")
WG_STATUS_KNOWN_HOSTS = os.environ.get(
    "WG_STATUS_KNOWN_HOSTS", str(BASE_DIR / "deploy" / "wg-status" / "known_hosts")
)
WG_STATUS_SSH_TIMEOUT = int(os.environ.get("WG_STATUS_SSH_TIMEOUT", "5"))
# A handshake is "connected" if it is within this many seconds of now.
WG_STATUS_FRESHNESS_SECONDS = int(os.environ.get("WG_STATUS_FRESHNESS_SECONDS", "180"))
# Per-peer cache TTL: a few minutes, so a DEAD poller expires to "unknown"
# (connected=null) rather than serving a stale "connected".
WG_STATUS_CACHE_TTL = int(os.environ.get("WG_STATUS_CACHE_TTL", "300"))
# Poll cadence (Celery beat), 30–60s.
WG_STATUS_POLL_INTERVAL = int(os.environ.get("WG_STATUS_POLL_INTERVAL", "45"))
# Feature flag: the poll task no-ops unless the staged status key is present.
WG_STATUS_ENABLED = os.environ.get("WG_STATUS_ENABLED", "1") not in (
    "0", "false", "no", "off",
)

# Login name baked into the student Kali template's cloud-init (ciuser), used to
# render the copy-paste SSH command on /my-lab/. The portal does NOT provision or
# store this — it is a property of template STUDENT_SOURCE_TEMPLATE, verified
# against the live VM config on 2026-07-15. If the template's ciuser changes,
# change this.
STUDENT_SSH_USER = os.environ.get("STUDENT_SSH_USER", "labadmin")

# --- Shared lab targets (B6.3, display-only) -------------------------------------
# The three FIXED, egress-locked vulnerable targets. Identical for every student
# (shared infra, NOT provisioned per-student), so this is a static config list
# rather than a DB table — there is no per-student state to model.
#
# Display-only: the portal never starts, stops, reaches or health-checks these.
# The pill each card shows is "Shared target", not a live status — nothing here
# is polled, so claiming "running" would be a guess.
#
# Names/VMIDs verified against the Proxmox API; ports verified by probe on
# 2026-07-15 (Juice Shop listens on :3000, NOT :80). Keep in sync by hand if the
# lab targets change.
SHARED_TARGETS = [
    {
        "name": "Metasploitable 2",
        "vmid": 103,
        "hostname": "metasploitable",
        "ip": "192.168.100.30",
        "blurb": "Classic vulnerable Linux host — services, not a web app.",
        "url": None,  # no web UI to link to
    },
    {
        "name": "DVWA",
        "vmid": 104,
        "hostname": "dvwa01",
        "ip": "192.168.100.40",
        "blurb": "Damn Vulnerable Web Application.",
        "url": "http://192.168.100.40/",
    },
    {
        "name": "OWASP Juice Shop",
        "vmid": 105,
        "hostname": "websec01",
        "ip": "192.168.100.41",
        "blurb": "Modern vulnerable web app (OWASP Top 10).",
        "url": "http://192.168.100.41:3000/",
    },
]

# --- Cache (shared, Redis) -------------------------------------------------------
# A SHARED cache is required: the poller runs in the worker/beat process and the
# reader (/api/my-lab) runs in the web process — different containers. LocMem
# would not be shared. Uses a SEPARATE redis DB (1) from Celery's broker (0).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("PORTAL_CACHE_URL", "redis://redis:6379/1"),
    }
}
