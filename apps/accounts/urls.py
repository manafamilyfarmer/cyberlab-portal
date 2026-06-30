from django.urls import path

from . import views

urlpatterns = [
    # JSON API
    path("api/auth/login", views.LoginView.as_view(), name="api-login"),
    path("api/auth/logout", views.LogoutView.as_view(), name="api-logout"),
    path("api/me", views.MeView.as_view(), name="api-me"),
    path("api/admin/ping", views.AdminPingView.as_view(), name="api-admin-ping"),
    path("api/staff/ping", views.StaffPingView.as_view(), name="api-staff-ping"),
    # Template (session) frontend
    path("accounts/login/", views.login_page, name="login-page"),
    path("accounts/me/", views.me_page, name="me-page"),
    path("accounts/totp/enroll/", views.totp_enroll, name="totp-enroll"),
]
