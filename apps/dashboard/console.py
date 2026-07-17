"""Instructor console context — the staff view of a running session (B6.4).

Same guarantee as apps/dashboard/scoping.py and apps/provisioning/mylab.py:
nothing here re-implements "which students may this instructor see". The roster
is derived from ``scoping.scoped_batches`` (the BatchViewSet queryset) and the
submissions from ``scoping.scoped_submissions`` (the SubmissionViewSet
queryset), so the console can never surface a student or a submission the
corresponding API would refuse this caller. Admin sees everything those
querysets return for an admin; an instructor sees their own batches only.

Data honesty — what is real, and how real (B6.4 Phase-0 inventory):

  * VPN pill      REAL + LIVE. The B4.5 vpn01 poll cache, read through
                  wgstatus.get_status_many(). Tri-state: a cache miss renders
                  "unknown", never "offline" — a dead poller is not a logout.
  * Box state     REAL but LAST-KNOWN, not live. VMInstance.proxmox_status is a
                  stored field written by the provisioning tasks when they
                  clone/start/stop a box. Nothing reconciles it against Proxmox
                  on a timer, so it can drift if a VM is changed outside the
                  portal. The template labels it "last known" for that reason —
                  it must not be read as a live hypervisor poll.
  * Submissions   REAL, including the ClamAV scan state.
  * Isolation     NO PORTAL-SIDE SOURCE. The portal only EMITS audit JSON toward
    alerts        the SIEM (apps/audit/emit.py); no WG-DROP / Wazuh / Security
                  Onion alert is ever read back into this database. There is
                  therefore no number to show and the console does not invent
                  one — see the "isolation" note in console.html.
"""
from django.db.models import Prefetch

from apps.accounts.models import StudentProfile
from apps.assessments.models import Assessment
from apps.labs.models import LabInstance, VMInstance, WireGuardPeer
from apps.provisioning import wgstatus

# Reuse the API's own definition of "a live per-student box" rather than
# restating the status list here — B3 Step 1 owns that rule.
from apps.provisioning.api import _LIVE_STUDENT_STATUSES

from . import scoping


def _initials(user):
    """Two-letter avatar initials from the real name, else the username."""
    first = (user.first_name or "").strip()
    last = (user.last_name or "").strip()
    if first or last:
        return ((first[:1] + last[:1]) or first[:2]).upper()
    return (user.username or "?")[:2].upper()


def _live_boxes_by_student(student_ids):
    """{student_id: LabInstance} for each student's live per-student box.

    One query for the whole roster, filtered by the same predicate
    MyLabViewSet._my_box() uses (per-student mode + a live status), so the
    console's idea of "their box" matches the student's own page. Ordered by
    created_at so the first row per student is the same one _my_box() picks.
    """
    boxes = (
        LabInstance.objects.filter(
            owner_student_id__in=student_ids,
            provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT,
            status__in=_LIVE_STUDENT_STATUSES,
        )
        .order_by("created_at")
        .prefetch_related(
            Prefetch("vms", queryset=VMInstance.objects.select_related("ip"))
        )
    )
    out = {}
    for box in boxes:
        out.setdefault(box.owner_student_id, box)
    return out


def _roster(request):
    """The rows of the live student table, scoped to what this caller may see.

    Students are those in the caller's scoped batches — never wider. Everything
    per row is resolved here (not in the template) so the template stays a
    render: VMInstance.ip is an FK to IPLease and would otherwise print a lease
    object, and the WireGuard peer is an optional reverse one-to-one.
    """
    batches = scoping.scoped_batches(request)
    students = list(
        StudentProfile.objects.filter(batches__in=batches)
        .distinct()
        .select_related("user")
        .order_by("student_index", "user__username")
    )
    student_ids = [s.id for s in students]

    peers = {
        p.student_id: p
        for p in WireGuardPeer.objects.filter(student_id__in=student_ids, active=True)
    }
    # One cache round-trip for the whole roster instead of N.
    statuses = wgstatus.get_status_many([p.id for p in peers.values()])
    boxes = _live_boxes_by_student(student_ids)

    rows = []
    for sp in students:
        peer = peers.get(sp.id)
        status = statuses.get(peer.id) if peer else None
        box = boxes.get(sp.id)
        vm = box.vms.all()[0] if box and box.vms.all() else None

        # The address the student actually connects to is the peer's kali_ip
        # (same resolution order as mylab.my_lab_context); the lease is the
        # fallback for a box with no peer yet.
        box_ip = peer.kali_ip if peer else None
        if not box_ip and vm is not None and vm.ip_id:
            box_ip = str(vm.ip.ip)

        rows.append(
            {
                "student": sp,
                "user": sp.user,
                "initials": _initials(sp.user),
                "tunnel_ip": peer.tunnel_ip if peer else None,
                "box_ip": box_ip,
                "has_peer": peer is not None,
                # True / False / None(=unknown, poller cache miss)
                "connected": status["connected"] if status else None,
                "last_handshake": status["last_handshake"] if status else None,
                "box": box,
                "vm": vm,
                # Last-known hypervisor state, NOT a live poll — see module docstring.
                "box_state": (vm.proxmox_status if vm else None),
            }
        )
    return rows


def _submissions_to_review(request):
    """Submissions in scope that still need a decision.

    "To review" = no FINAL assessment yet (never assessed, or assessed and left
    in draft). Reuses the SubmissionViewSet queryset, so an instructor only ever
    sees submissions from students in their own batches.
    """
    return (
        scoping.scoped_submissions(request)
        .exclude(assessment__status=Assessment.Status.FINAL)
        .select_related("student__user", "lab_exercise", "lab_exercise__module")
        .order_by("-submitted_at")
    )


def console_context(request):
    """Everything the instructor console renders. Every value traces to a real
    query; sections with no source render an empty state rather than filler."""
    rows = _roster(request)
    submissions = list(_submissions_to_review(request))
    batches = list(scoping.scoped_batches(request).select_related("course"))

    return {
        "batches": batches,
        # The console is built for a single running cohort; the pilot has exactly
        # one batch. If a caller (e.g. admin) scopes to several, the header names
        # the first and the count tells the truth about the rest.
        "batch": batches[0] if batches else None,
        "rows": rows,
        "submissions": submissions,
        "tiles": {
            "connected_now": sum(1 for r in rows if r["connected"] is True),
            "status_unknown": sum(1 for r in rows if r["connected"] is None),
            "boxes_running": sum(1 for r in rows if r["box_state"] == "running"),
            "students_total": len(rows),
            "to_review": len(submissions),
            # NB: deliberately no "isolation_alerts" key. There is no portal-side
            # source for it (see module docstring) and a fabricated 0 would read
            # as "no alerts" rather than "not wired up".
        },
    }


def student_detail_context(request, student_id):
    """One student's row + their submissions, for the staff drill-down.

    The student is looked up in the caller's OWN roster, so an out-of-scope id
    is simply not found (returns None -> the view 404s). That mirrors the
    object-isolation rule the read APIs use: out of scope is indistinguishable
    from nonexistent.
    """
    row = next((r for r in _roster(request) if r["student"].id == student_id), None)
    if row is None:
        return None
    submissions = (
        scoping.scoped_submissions(request)
        .filter(student_id=student_id)
        .select_related("lab_exercise", "lab_exercise__module")
        .order_by("-submitted_at")
    )
    return {"row": row, "submissions": list(submissions)}
