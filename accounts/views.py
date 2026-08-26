from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from accounts.permissions import IsStaffUser

from .serializers import ChangePasswordSerializer, UserSerializer

User = get_user_model()


class LoginView(TokenObtainPairView):
    """POST username/password → access + refresh JWT."""


class RefreshView(TokenRefreshView):
    """POST refresh → new access JWT."""


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class StaffUsersView(APIView):
    """Compact staff directory for operator-wallet assignment."""

    permission_classes = [IsStaffUser]

    def get(self, request):
        qs = (
            User.objects.filter(is_active=True)
            .filter(Q(is_staff=True) | Q(is_superuser=True) | Q(groups__isnull=False))
            .distinct()
            .order_by("username")
            .only("id", "username", "first_name", "last_name")
        )
        rows = []
        for u in qs[:200]:
            full = f"{u.first_name} {u.last_name}".strip()
            rows.append(
                {
                    "id": u.id,
                    "username": u.username,
                    "label": f"{u.username} ({full})" if full else u.username,
                }
            )
        return Response(rows)


class ChangePasswordView(APIView):
    """POST current + new password for the authenticated staff user."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        return Response(
            {"detail": "Password updated."},
            status=status.HTTP_200_OK,
        )
