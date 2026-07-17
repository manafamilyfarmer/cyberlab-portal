"""Template dashboards + audit view (session frontend).

Role-routed: ``request.user.role`` picks the template. All context is built from
the same scoped querysets the APIs use (apps.dashboard.scoping / .summary), so a
template can never render rows the role can't see. Admin/instructor pages are
staff-MFA-gated (403 until a TOTP device is verified this session), mirroring the
StaffMFARequired API permission.
"""
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import render

from . import console as console_ctx
from . import scoping
from .summary import build_summary

_ROLE_TEMPLATE = {
    "admin": "dashboard/admin.html",
    "instructor": "dashboard/instructor.html",
    "student": "dashboard/student.html",
}


def _staff_mfa_forbidden(user):
    """Return an HttpResponseForbidden if a staff user lacks verified MFA, else
    None. Same predicate as the StaffMFARequired DRF permission."""
    if getattr(user, "is_staff_role", False):
        is_verified = getattr(user, "is_verified", None)
        if not (callable(is_verified) and is_verified()):
            return HttpResponseForbidden("Staff MFA (TOTP) verification required.")
    return None


@login_required
def dashboard(request):
    user = request.user
    forbidden = _staff_mfa_forbidden(user)
    if forbidden is not None:
        return forbidden
    template = _ROLE_TEMPLATE.get(getattr(user, "role", None), "dashboard/guest.html")
    return render(
        request,
        template,
        {"summary": build_summary(request), "user_obj": user},
    )


@login_required
def audit_page(request):
    user = request.user
    if getattr(user, "role", None) not in ("admin", "instructor"):
        return HttpResponseForbidden("Admin or instructor role required.")
    forbidden = _staff_mfa_forbidden(user)
    if forbidden is not None:
        return forbidden
    rows = scoping.scoped_audit(request).select_related("actor")[:200]
    return render(request, "dashboard/audit.html", {"rows": rows, "user_obj": user})


def _staff_page_forbidden(user):
    """The gate every staff-only console page uses: admin/instructor role AND a
    verified TOTP device this session.

    This is the SAME predicate pair the staff APIs enforce (IsAdminOrInstructor
    + StaffMFARequired) and that audit_page already applies. A student hitting a
    console URL is refused here on role, before any queryset is built.
    """
    if getattr(user, "role", None) not in ("admin", "instructor"):
        return HttpResponseForbidden("Admin or instructor role required.")
    return _staff_mfa_forbidden(user)


@login_required
def instructor_console(request):
    """/instructor/ — the staff console for a running session (B6.4).

    Anonymous -> @login_required redirects to LOGIN_URL.
    Student    -> 403 (role gate).
    Staff without verified MFA -> 403 (same as every other staff page).
    """
    forbidden = _staff_page_forbidden(request.user)
    if forbidden is not None:
        return forbidden
    ctx = console_ctx.console_context(request)
    ctx["user_obj"] = request.user
    return render(request, "dashboard/console.html", ctx)


@login_required
def instructor_student_detail(request, student_id):
    """/instructor/student/<id>/ — staff drill-down on one student.

    Same gate as the console. The student is resolved from the caller's own
    scoped roster, so an out-of-scope id 404s rather than leaking existence.
    """
    forbidden = _staff_page_forbidden(request.user)
    if forbidden is not None:
        return forbidden
    ctx = console_ctx.student_detail_context(request, student_id)
    if ctx is None:
        raise Http404("No such student in your scope.")
    ctx["user_obj"] = request.user
    return render(request, "dashboard/console_student.html", ctx)
