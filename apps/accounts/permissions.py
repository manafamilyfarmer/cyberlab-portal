"""Reusable DRF permission classes — RBAC lives here, not in templates.

Other apps import these. Role checks read User.role; the staff-MFA gate reads
django_otp's request.user.is_verified().
"""
from rest_framework.permissions import BasePermission


def _role(request):
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return None
    return getattr(user, "role", None)


class IsAdmin(BasePermission):
    message = "Admin role required."

    def has_permission(self, request, view):
        return _role(request) == "admin"


class IsInstructor(BasePermission):
    message = "Instructor role required."

    def has_permission(self, request, view):
        return _role(request) == "instructor"


class IsStudent(BasePermission):
    message = "Student role required."

    def has_permission(self, request, view):
        return _role(request) == "student"


class IsAdminOrInstructor(BasePermission):
    message = "Admin or instructor role required."

    def has_permission(self, request, view):
        return _role(request) in ("admin", "instructor")


class IsOwnerOrStaff(BasePermission):
    """Object-level: the object's owner, or an admin/instructor."""

    message = "You may only act on your own resources."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if _role(request) in ("admin", "instructor"):
            return True
        owner_id = getattr(obj, "user_id", None)
        return owner_id is not None and owner_id == user.id


class StaffMFARequired(BasePermission):
    """Admin/instructor must have a verified TOTP device (django_otp).

    Non-staff roles are unaffected. Relies on OTPMiddleware having attached
    is_verified() to request.user.
    """

    message = "Staff MFA (TOTP) verification required."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False
        if getattr(user, "is_staff_role", False):
            is_verified = getattr(user, "is_verified", None)
            return bool(callable(is_verified) and is_verified())
        return True
