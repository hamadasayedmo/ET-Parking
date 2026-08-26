from rest_framework.permissions import BasePermission, IsAuthenticated, SAFE_METHODS

OPS_ROLE_GROUPS = ("Operations", "Wallet", "Control Room")
CONTROL_ROOM_GROUP = "Control Room"


def user_in_ops_role(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user.groups.filter(name__in=OPS_ROLE_GROUPS).exists()


def user_can_decide_access(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if user.has_perm("parking.decide_accessrequest"):
        return True
    return user.groups.filter(name=CONTROL_ROOM_GROUP).exists()


class IsStaffUser(BasePermission):
    """
    Ops API access: Django staff flag, or an ops role group
    (Operations / Wallet / Control Room).
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser or user.is_staff:
            return True
        return user_in_ops_role(user)


class IsStaffReadSuperuserWrite(IsStaffUser):
    """Staff/ops may read; only superusers may create/update/delete."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_superuser)


class CanDecideAccessRequest(IsAuthenticated):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return user_can_decide_access(request.user)


class CanAdjustWallet(IsAuthenticated):
    """Top-up / withdraw require Wallet group permission (or superuser)."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        user = request.user
        if user.is_superuser:
            return True
        if not (user.is_staff or user_in_ops_role(user)):
            return False
        return user.has_perm("wallets.adjust_wallet")


class CanViewWalletLedger(IsAuthenticated):
    """
    Wallet / transaction reads: Operations or Wallet groups (or adjust perm).
    Writes still go through CanAdjustWallet on the action.
    """

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        user = request.user
        if user.is_superuser:
            return True
        if not (user.is_staff or user_in_ops_role(user)):
            return False
        if request.method in SAFE_METHODS:
            return (
                user.groups.filter(name__in=OPS_ROLE_GROUPS).exists()
                or user.has_perm("wallets.adjust_wallet")
                or user.has_perm("wallets.view_wallet")
            )
        return user.has_perm("wallets.adjust_wallet")
