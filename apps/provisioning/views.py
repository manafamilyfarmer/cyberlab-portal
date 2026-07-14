"""Session (template) frontend for a student's own lab — B6.3.

The page is a RENDER of the same data GET /api/my-lab/ serves: it calls into
apps.provisioning.mylab, which drives MyLabViewSet's own helpers with the live
request. There is no second copy of the "which box is mine" rule here.
"""
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.dashboard import scoping

from .mylab import my_lab_context


def _latest_submission_by_exercise(request):
    """Map {lab_exercise_id: Submission} using the caller's OWN submissions.

    Reuses the submissions API queryset (student -> filter(student=sp)), so a
    student can only ever key off their own rows. Submission.Meta.ordering is
    ("-submitted_at",) and a student may submit the same exercise repeatedly, so
    the FIRST row seen per exercise is the latest one.
    """
    latest = {}
    for sub in scoping.scoped_submissions(request).order_by("-submitted_at"):
        latest.setdefault(sub.lab_exercise_id, sub)
    return latest


@login_required
def my_lab(request):
    """/my-lab/ — the student's own lab. Login-required; students only.

    Anonymous -> @login_required redirects to LOGIN_URL.
    Non-student role -> 403 page (mirrors the API's "Students only." 403).
    """
    if getattr(request.user, "role", None) != "student":
        return render(
            request,
            "my-lab-denied.html",
            {"role": getattr(request.user, "role", None)},
            status=403,
        )

    ctx = my_lab_context(request)

    # Exercises: REAL, but course-scoped only. There is no per-student progress
    # model anywhere in the portal (no Progress/Completion/Attempt table, and
    # Batch.students is a bare M2M with no through-model), so this list is the
    # same for every student on the course and carries NO completion state.
    # The only genuine per-student signal is "did I submit?" — derived from real
    # Submission rows, which is submission state, NOT progress/marks.
    exercises = list(
        scoping.scoped_exercises(request)
        .select_related("module", "module__course")
        .order_by("module__order", "module__code", "title")
    )
    latest = _latest_submission_by_exercise(request)
    for ex in exercises:
        ex.my_submission = latest.get(ex.id)

    ctx.update(
        {
            "exercises": exercises,
            "shared_targets": settings.SHARED_TARGETS,
            "ssh_user": settings.STUDENT_SSH_USER,
        }
    )
    return render(request, "my-lab.html", ctx)
