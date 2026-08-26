"""Helpers shared by kiosk REST + SSE views."""

from __future__ import annotations

from rest_framework.response import Response
from rest_framework import status

from payments.models import KioskCredential
from sites.models import Device


def resolve_kiosk_device(request, *, require_device_param_for_staff=True):
    """
    Returns (device, credential_or_None, error_response_or_None).

    - Kiosk token: device comes from the credential (optional ?device= must match).
    - Staff JWT: ?device= required.
    """
    credential = request.auth if isinstance(request.auth, KioskCredential) else None

    if credential is not None:
        device = credential.device
        if device.type != Device.Type.EXIT:
            return None, None, Response(
                {"detail": "Kiosk credential is not linked to an exit device."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw = request.query_params.get("device") or (
            request.data.get("device") if hasattr(request, "data") else None
        )
        if raw not in (None, ""):
            try:
                if int(raw) != device.pk:
                    return None, None, Response(
                        {"detail": "device does not match kiosk credential."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            except (TypeError, ValueError):
                return None, None, Response(
                    {"detail": "device must be an integer id."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return device, credential, None

    if not require_device_param_for_staff:
        return None, None, Response(
            {"detail": "Kiosk token required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    raw = request.query_params.get("device") or (
        request.data.get("device") if hasattr(request, "data") else None
    )
    if not raw:
        return None, None, Response(
            {"detail": "device is required (exit device id)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        device_pk = int(raw)
    except (TypeError, ValueError):
        return None, None, Response(
            {"detail": "device must be an integer id."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    device = (
        Device.objects.select_related("site", "site__project")
        .filter(pk=device_pk, type=Device.Type.EXIT)
        .first()
    )
    if device is None:
        return None, None, Response(
            {"detail": "Exit device not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return device, None, None
