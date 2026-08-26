from rest_framework.permissions import BasePermission

from accounts.permissions import IsStaffUser

from .models import KioskCredential


class IsKioskCredential(BasePermission):
    """Authenticated via KioskTokenAuthentication (request.auth is KioskCredential)."""

    message = (
        "Kiosk API token required. Create one in Admin → Payments → "
        "Kiosk credentials and pair the tablet (do not use a staff JWT)."
    )

    def has_permission(self, request, view):
        return isinstance(getattr(request, "auth", None), KioskCredential)


class IsKioskOrStaff(BasePermission):
    """Kiosk token or staff user."""

    def has_permission(self, request, view):
        if isinstance(getattr(request, "auth", None), KioskCredential):
            return True
        return IsStaffUser().has_permission(request, view)
