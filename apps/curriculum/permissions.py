"""Object-level batch permission: admins manage any batch; an instructor may
only manage batches they own (Batch.instructor == their InstructorProfile).
"""
from rest_framework.permissions import BasePermission


def _role(request):
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return None
    return getattr(user, "role", None)


class CanManageBatch(BasePermission):
    message = "Admins, or the owning instructor, may manage this batch."

    def has_permission(self, request, view):
        return _role(request) in ("admin", "instructor")

    def has_object_permission(self, request, view, obj):
        if _role(request) == "admin":
            return True
        instructor_profile = getattr(request.user, "instructor_profile", None)
        return (
            instructor_profile is not None
            and obj.instructor_id == instructor_profile.id
        )
