"""Test settings — hermetic, no external services.

The portal DB user (portaluser) has NO CREATEDB right on portaldb, so the Django
test runner cannot spin up a Postgres test database. Tests therefore run on an
in-memory SQLite database and a temp audit-log path. WG_SECRETS_DIR is pointed at
a throwaway temp dir per test (via override_settings) holding FAKE fixture
configs — no real secret ever touches the test suite.
"""
import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-insecure-do-not-use")
os.environ.setdefault("DJANGO_DEBUG", "0")
os.environ.setdefault("PORTAL_DB_NAME", "test")
os.environ.setdefault("PORTAL_DB_USER", "test")
os.environ.setdefault("PORTAL_DB_PASSWORD", "test")
os.environ.setdefault("PORTAL_DB_HOST", "127.0.0.1")
os.environ.setdefault("PORTAL_DB_PORT", "5432")

from .base import *  # noqa: E402,F401,F403

DEBUG = False

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Keep the audit JSON emit writable and out of the way during tests.
_AUDIT_TMP = tempfile.mkdtemp(prefix="cyberlab-test-audit-")
AUDIT_LOG_DIR = _AUDIT_TMP
AUDIT_LOG_PATH = os.path.join(_AUDIT_TMP, "audit.jsonl")

# Default WG dir for tests (individual tests override with a fixture dir).
WG_SECRETS_DIR = tempfile.mkdtemp(prefix="cyberlab-test-wg-")

# Don't let django-axes touch the DB backend during unit tests.
AXES_ENABLED = False

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
