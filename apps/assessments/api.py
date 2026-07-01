"""assessments API — hostile-upload pipeline + secure download + grading.

Every uploaded file is treated as hostile: type+size allowlisted BEFORE storing,
stored outside the web root with a uuid name at mode 0600, sha256 recorded, never
executed, scanned async by ClamAV, and served only as an octet-stream attachment.
"""
import hashlib
import os
import uuid

from django.conf import settings
from django.http import FileResponse
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdminOrInstructor, IsStudent, StaffMFARequired
from apps.audit.services import write_audit
from apps.curriculum.models import Batch, LabExercise

from .models import Assessment, Submission
from .serializers import AssessmentSerializer, SubmissionSerializer
from .tasks import scan_submission


def _profiles(request):
    return (
        getattr(request.user, "instructor_profile", None),
        getattr(request.user, "student_profile", None),
    )


def _role(request):
    return getattr(request.user, "role", None)


class SubmissionViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = SubmissionSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated(), IsStudent()]
        return [IsAuthenticated()]

    def get_queryset(self):
        role = _role(self.request)
        if role == "admin":
            return Submission.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return Submission.objects.filter(student__batches__instructor=ip).distinct()
        if role == "student" and sp is not None:
            return Submission.objects.filter(student=sp)
        return Submission.objects.none()

    def create(self, request, *args, **kwargs):
        _, sp = _profiles(request)
        if sp is None:
            raise PermissionDenied("No student profile.")

        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "No file provided."})

        exercise_id = request.data.get("lab_exercise")
        try:
            exercise = LabExercise.objects.get(pk=exercise_id)
        except (LabExercise.DoesNotExist, ValueError, TypeError):
            raise ValidationError({"lab_exercise": "Invalid lab_exercise."})

        # Enrolled-in-the-exercise check.
        if not Batch.objects.filter(
            students=sp, course__modules__exercises=exercise
        ).exists():
            raise PermissionDenied("You are not enrolled in a batch for this exercise.")

        # --- HOSTILE-UPLOAD VALIDATION FIRST (reject before storing) ---
        if upload.content_type not in settings.SUBMISSION_ALLOWED_TYPES:
            raise ValidationError(
                {"file": f"Disallowed content type: {upload.content_type}"}
            )
        if upload.size > settings.SUBMISSION_MAX_BYTES:
            raise ValidationError(
                {"file": f"File too large (> {settings.SUBMISSION_MAX_BYTES} bytes)."}
            )

        # --- Store outside the web root, uuid name, mode 0600, no execute ---
        os.makedirs(settings.SUBMISSIONS_DIR, exist_ok=True)
        stored_name = uuid.uuid4().hex
        stored_path = os.path.join(settings.SUBMISSIONS_DIR, stored_name)
        sha = hashlib.sha256()
        size = 0
        # Open with restrictive perms from the start (0600).
        fd = os.open(stored_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in upload.chunks():
                    out.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
        except Exception:
            if os.path.exists(stored_path):
                os.remove(stored_path)
            raise
        os.chmod(stored_path, 0o600)  # belt-and-suspenders: no execute bit

        submission = Submission.objects.create(
            student=sp,
            lab_exercise=exercise,
            stored_path=stored_path,
            original_filename=upload.name[:255],
            content_type=upload.content_type or "application/octet-stream",
            size=size,
            sha256=sha.hexdigest(),
            scan_status=Submission.ScanStatus.PENDING,
        )
        write_audit(
            request.user, "submission.create", request=request,
            target_type="Submission", target_id=submission.pk,
            sha256=submission.sha256, size=size,
        )
        scan_submission.delay(submission.pk)

        data = SubmissionSerializer(submission).data
        return Response(data, status=201)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        submission = self.get_object()  # 404 if outside the caller's role queryset
        role = _role(request)
        is_owner = (
            role == "student"
            and getattr(request.user, "student_profile", None)
            and submission.student_id == request.user.student_profile.id
        )

        if not is_owner:
            # Instructor/admin: scan gate — only clean files may be downloaded.
            if submission.scan_status == Submission.ScanStatus.INFECTED:
                return Response({"detail": "File is quarantined (infected)."}, status=403)
            if submission.scan_status != Submission.ScanStatus.CLEAN:
                return Response(
                    {"detail": f"File not available for download (scan_status="
                               f"{submission.scan_status})."},
                    status=409,
                )

        if not os.path.exists(submission.stored_path):
            return Response({"detail": "Stored file missing."}, status=410)

        resp = FileResponse(
            open(submission.stored_path, "rb"),
            as_attachment=True,
            filename=submission.original_filename,
            content_type="application/octet-stream",
        )
        resp["X-Content-Type-Options"] = "nosniff"
        if is_owner and submission.scan_status != Submission.ScanStatus.CLEAN:
            resp["X-Scan-Status"] = submission.scan_status  # owner-visible warning
        return resp


# --------------------------------------------------------------------------- #
# Assessment (grading)                                                          #
# --------------------------------------------------------------------------- #
class AssessmentReadViewSet(viewsets.ReadOnlyModelViewSet):
    """Students see their OWN final marks; instructors their batches'; admin all."""

    serializer_class = AssessmentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        role = _role(self.request)
        if role == "admin":
            return Assessment.objects.all()
        ip, sp = _profiles(self.request)
        if role == "instructor" and ip is not None:
            return Assessment.objects.filter(
                submission__student__batches__instructor=ip
            ).distinct()
        if role == "student" and sp is not None:
            return Assessment.objects.filter(
                submission__student=sp, status=Assessment.Status.FINAL
            )
        return Assessment.objects.none()


class AdminAssessmentViewSet(viewsets.ModelViewSet):
    serializer_class = AssessmentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrInstructor, StaffMFARequired]

    def get_queryset(self):
        if _role(self.request) == "instructor":
            ip, _ = _profiles(self.request)
            if ip is None:
                return Assessment.objects.none()
            return Assessment.objects.filter(
                submission__student__batches__instructor=ip
            ).distinct()
        return Assessment.objects.all()

    def _check_scope(self, submission):
        if _role(self.request) != "instructor":
            return
        ip, _ = _profiles(self.request)
        if not Batch.objects.filter(
            instructor=ip, students=submission.student
        ).exists():
            raise PermissionDenied("Instructors may only grade their own batches' students.")

    def _save(self, serializer, suffix):
        from django.utils import timezone

        submission = serializer.validated_data.get(
            "submission", getattr(serializer.instance, "submission", None)
        )
        self._check_scope(submission)
        ip, _ = _profiles(self.request)
        obj = serializer.save(instructor=ip, graded_at=timezone.now())
        write_audit(
            self.request.user, f"assessment.{suffix}", request=self.request,
            target_type="Assessment", target_id=obj.pk,
        )

    def perform_create(self, serializer):
        self._save(serializer, "create")

    def perform_update(self, serializer):
        self._save(serializer, "update")

    def perform_destroy(self, instance):
        target_id = instance.pk
        instance.delete()
        write_audit(
            self.request.user, "assessment.delete", request=self.request,
            target_type="Assessment", target_id=target_id,
        )
