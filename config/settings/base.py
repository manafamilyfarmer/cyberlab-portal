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
        "DIRS": [],
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

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

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
