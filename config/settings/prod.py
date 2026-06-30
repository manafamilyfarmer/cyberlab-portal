"""Production settings.

All secrets come from the environment (VM114 env_files). No insecure defaults.
Transport-security headers are defined now but gated on DJANGO_SECURE so the
internal-HTTP smoke test works until TLS / the access model lands at B2.
"""
import os

from .base import *  # noqa: F401,F403

DEBUG = False

# Flip to "1" once TLS terminates in front of the stack (B2).
_SECURE = os.environ.get("DJANGO_SECURE", "0") == "1"

# Cookies
SESSION_COOKIE_SECURE = _SECURE
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = _SECURE
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

# Transport security
SECURE_SSL_REDIRECT = _SECURE
SECURE_HSTS_SECONDS = 31536000 if _SECURE else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = _SECURE
SECURE_HSTS_PRELOAD = _SECURE
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
# Behind the B2 reverse proxy, trust its forwarded-proto header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CSRF_TRUSTED_ORIGINS = [
    "https://192.168.100.92",
    "http://192.168.100.92:8000",
]
