"""Auth surface: session login/logout, /api/me, RBAC + MFA probe endpoints,
plus minimal template views for the session frontend and TOTP enrollment.
"""
from axes.handlers.proxy import AxesProxyHandler
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit

from .permissions import IsAdmin, IsAdminOrInstructor, StaffMFARequired
from .serializers import MeSerializer


def _confirmed_totp(user):
    return TOTPDevice.objects.filter(user=user, confirmed=True).first()


class LoginView(APIView):
    """POST username/password (+optional otp_token).

    Runs through django-axes (AxesStandaloneBackend). On success for staff
    roles, an OTP step is required: pass otp_token to verify, otherwise the
    response signals that MFA verification or enrollment is needed.
    """

    permission_classes = [AllowAny]
    authentication_classes = []  # login establishes the session itself

    def post(self, request):
        username = request.data.get("username", "")
        password = request.data.get("password", "")
        otp_token = request.data.get("otp_token")
        credentials = {"username": username}

        locked_response = Response(
            {
                "detail": "Account temporarily locked due to too many failed "
                          "login attempts.",
                "cooloff_hours": 1,
            },
            status=403,
        )

        # Axes lockout: django.contrib.auth.authenticate() swallows the
        # backend's PermissionDenied and returns None, so check explicitly.
        if AxesProxyHandler.is_locked(request, credentials=credentials):
            return locked_response

        try:
            user = authenticate(request, username=username, password=password)
        except PermissionDenied:
            return locked_response

        if user is None:
            # A failed attempt may have just tripped the lockout threshold.
            if AxesProxyHandler.is_locked(request, credentials=credentials):
                return locked_response
            # user_login_failed signal records the audit row.
            return Response({"detail": "Invalid credentials."}, status=401)

        login(request, user)

        mfa_state = "not_required"
        if getattr(user, "is_staff_role", False):
            device = _confirmed_totp(user)
            if device is None:
                mfa_state = "enrollment_required"
            elif otp_token and device.verify_token(otp_token):
                otp_login(request, device)
                mfa_state = "verified"
            else:
                mfa_state = "required"

        return Response(
            {
                "username": user.username,
                "role": user.role,
                "mfa_enabled": user.mfa_enabled,
                "mfa_state": mfa_state,
                # otp_device is set by otp_login() on successful TOTP verify.
                "is_verified": bool(getattr(request.user, "otp_device", None)),
            },
            status=200,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        write_audit(request.user, "auth.logout", request=request)
        logout(request)
        return Response(status=204)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MeSerializer(request.user).data)


class AdminPingView(APIView):
    """RBAC probe: admin-only. Proves IsAdmin returns 403 for non-admins."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        return Response({"pong": True, "role": request.user.role})


class StaffPingView(APIView):
    """MFA-gate probe: admin/instructor AND a verified TOTP device."""

    permission_classes = [IsAuthenticated, IsAdminOrInstructor, StaffMFARequired]

    def get(self, request):
        return Response({"pong": True, "role": request.user.role, "mfa": "verified"})


# --- Minimal template views (session frontend) ---

def login_page(request):
    """Basic /accounts/login/ form (functional, not polished)."""
    error = None
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        try:
            user = authenticate(request, username=username, password=password)
        except PermissionDenied:
            error = "Account locked. Try again later."
            user = None
        if user is not None:
            login(request, user)
            if getattr(user, "is_staff_role", False) and _confirmed_totp(user) is None:
                return redirect("totp-enroll")
            return redirect("me-page")
        elif error is None:
            error = "Invalid credentials."
    return render(request, "accounts/login.html", {"error": error})


@login_required
def me_page(request):
    return render(request, "accounts/me.html", {"user_obj": request.user})


@login_required
def totp_enroll(request):
    """Create/confirm a TOTP device; render a QR for enrollment."""
    import io
    import base64

    import qrcode

    device = TOTPDevice.objects.filter(user=request.user, confirmed=False).first()
    if device is None and _confirmed_totp(request.user) is None:
        device = TOTPDevice.objects.create(
            user=request.user, name="default", confirmed=False
        )

    message = None
    if request.method == "POST" and device is not None:
        token = request.POST.get("token", "")
        if device.verify_token(token):
            device.confirmed = True
            device.save()
            request.user.mfa_enabled = True
            request.user.save(update_fields=["mfa_enabled"])
            otp_login(request, device)
            write_audit(request.user, "auth.mfa_enrolled", request=request)
            return redirect("me-page")
        message = "Invalid token, try again."

    qr_data_uri = None
    if device is not None:
        img = qrcode.make(device.config_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    if device is None:
        return HttpResponse("TOTP already enrolled.", content_type="text/plain")

    return render(
        request,
        "accounts/totp_enroll.html",
        {"qr_data_uri": qr_data_uri, "message": message},
    )
