"""Role summary aggregation for GET /api/dashboard/ and the template dashboards.

Every count is derived from the reused role-scoped querysets in ``scoping`` so
the summary can never surface data the caller's role couldn't already fetch from
the corresponding API. The same ``build_summary`` output feeds both the JSON
endpoint and the rendered dashboard, so the two can never disagree.
"""
from apps.accounts.models import StudentProfile
from apps.labs.models import IPLease

from . import scoping


def _submission_breakdown(qs):
    return {
        "total": qs.count(),
        "clean": qs.filter(scan_status="clean").count(),
        "pending": qs.filter(scan_status="pending").count(),
        "infected": qs.filter(scan_status="infected").count(),
        "error": qs.filter(scan_status="error").count(),
    }


def build_summary(request):
    user = request.user
    role = getattr(user, "role", None)
    summary = {"username": user.username, "role": role}

    if role == "student":
        summary["student"] = {
            "batches": scoping.scoped_batches(request).count(),
            "exercises": scoping.scoped_exercises(request).count(),
            "submissions": _submission_breakdown(scoping.scoped_submissions(request)),
            "assessments_final": scoping.scoped_assessments(request).count(),
            "active_schedules": scoping.scoped_schedules(request)
            .filter(is_active=True)
            .count(),
            "access_sessions": scoping.scoped_sessions(request).count(),
            "reservations": scoping.scoped_reservations(request).count(),
        }
    elif role == "instructor":
        batches = scoping.scoped_batches(request)
        summary["instructor"] = {
            "batches": batches.count(),
            # Students implied by (and never wider than) the instructor's own batches.
            "students": StudentProfile.objects.filter(batches__in=batches)
            .distinct()
            .count(),
            "exercises": scoping.scoped_exercises(request).count(),
            "submissions": _submission_breakdown(scoping.scoped_submissions(request)),
            "assessments": scoping.scoped_assessments(request).count(),
            "schedules": scoping.scoped_schedules(request).count(),
            "access_sessions": scoping.scoped_sessions(request).count(),
            "audit_events_visible": scoping.scoped_audit(request).count(),
        }
    elif role == "admin":
        leases = IPLease.objects.all()
        summary["admin"] = {
            "courses": scoping.scoped_courses(request).count(),
            "batches": scoping.scoped_batches(request).count(),
            "exercises": scoping.scoped_exercises(request).count(),
            "students": StudentProfile.objects.count(),
            "submissions": _submission_breakdown(scoping.scoped_submissions(request)),
            "assessments": scoping.scoped_assessments(request).count(),
            "schedules": scoping.scoped_schedules(request).count(),
            "access_sessions": scoping.scoped_sessions(request).count(),
            "ip_pool": {
                "total": leases.count(),
                "free": leases.filter(state="free").count(),
                "leased": leases.filter(state="leased").count(),
            },
            "audit_events": scoping.scoped_audit(request).count(),
        }
    else:
        summary["guest"] = {"message": "No role-specific dashboard."}
    return summary
