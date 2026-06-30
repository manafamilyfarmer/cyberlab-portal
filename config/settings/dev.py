"""Local development settings.

Seeds insecure defaults for env vars BEFORE importing base, so a developer can
run manage.py locally without the VM114 secret files. These defaults are never
used in the container, which loads config.settings.prod with real env_files.
"""
import os

os.environ.setdefault("DJANGO_SECRET_KEY", "dev-insecure-do-not-use-in-prod")
os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("PORTAL_DB_NAME", "portaldb")
os.environ.setdefault("PORTAL_DB_USER", "portaluser")
os.environ.setdefault("PORTAL_DB_PASSWORD", "")
os.environ.setdefault("PORTAL_DB_HOST", "127.0.0.1")
os.environ.setdefault("PORTAL_DB_PORT", "5432")
os.environ.setdefault("PORTAL_DB_SSLMODE", "require")

from .base import *  # noqa: E402,F401,F403

DEBUG = True
