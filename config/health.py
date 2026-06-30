"""Liveness/readiness endpoints.

healthz: process is up (no dependencies touched).
readyz : the TLS path to portaldb works (SELECT 1), else 503.
"""
from django.db import connection
from django.http import HttpResponse, JsonResponse


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


def readyz(request):
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — report any DB failure as not-ready
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)
    return JsonResponse({"status": "ready"}, status=200)
