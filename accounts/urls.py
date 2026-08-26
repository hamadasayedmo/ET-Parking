from django.urls import path

from .views import (
    ChangePasswordView,
    LoginView,
    MeView,
    RefreshView,
    StaffUsersView,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="auth-login"),
    path("refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("me/", MeView.as_view(), name="auth-me"),
    path("staff/", StaffUsersView.as_view(), name="auth-staff"),
    path(
        "change-password/",
        ChangePasswordView.as_view(),
        name="auth-change-password",
    ),
]
