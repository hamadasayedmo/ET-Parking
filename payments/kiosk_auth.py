"""Kiosk device token authentication (no staff user required)."""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework import authentication, exceptions

from payments.models import KioskCredential


class KioskAuthUser(AnonymousUser):
    """
    Stand-in user so DRF IsAuthenticated does not 403 kiosk tokens.
    Real authorization is request.auth (KioskCredential).
    """

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def __str__(self):
        return "KioskAuthUser"


class KioskTokenAuthentication(authentication.BaseAuthentication):
    """
    Accept:
      Authorization: Bearer <kiosk_token>
      X-Kiosk-Token: <kiosk_token>
    """

    keyword = "bearer"

    def authenticate(self, request):
        raw = self._extract_token(request)
        if not raw:
            return None

        credential = (
            KioskCredential.objects.select_related(
                "device",
                "device__site",
                "device__site__project",
            )
            .filter(token=raw, is_active=True)
            .first()
        )
        if credential is None:
            raise exceptions.AuthenticationFailed(
                "Invalid kiosk token. Use the token from Admin → "
                "Payments → Kiosk credentials (not a staff JWT)."
            )

        KioskCredential.objects.filter(pk=credential.pk).update(
            last_used_at=timezone.now()
        )
        return (KioskAuthUser(), credential)

    def _extract_token(self, request) -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return (request.headers.get("X-Kiosk-Token") or "").strip()
