"""Mint and redeem short-lived kiosk pairing codes."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from .models import KioskCredential, KioskPairingCode, generate_pairing_code

DEFAULT_TTL_SECONDS = 15 * 60


def mint_pairing_code(
    *,
    credential: KioskCredential,
    created_by=None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> KioskPairingCode:
    if not credential.is_active:
        raise ValidationError({"credential_id": "Kiosk credential is inactive."})

    ttl = max(60, min(int(ttl_seconds), 24 * 3600))
    now = timezone.now()
    expires_at = now + timedelta(seconds=ttl)

    with transaction.atomic():
        # One open code per credential — revoke older unused ones.
        KioskPairingCode.objects.filter(
            credential=credential,
            used_at__isnull=True,
            expires_at__gt=now,
        ).update(expires_at=now)

        code = generate_pairing_code()
        for _ in range(8):
            if not KioskPairingCode.objects.filter(code=code).exists():
                break
            code = generate_pairing_code()
        else:
            raise ValidationError({"detail": "Could not allocate a unique pairing code."})

        return KioskPairingCode.objects.create(
            credential=credential,
            code=code,
            expires_at=expires_at,
            created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
        )


def redeem_pairing_code(raw_code: str) -> dict:
    code = (raw_code or "").strip().upper().replace(" ", "").replace("-", "")
    if len(code) < 4:
        raise ValidationError({"code": "Enter the pairing code from admin."})

    with transaction.atomic():
        row = (
            KioskPairingCode.objects.select_for_update()
            .select_related("credential", "credential__device", "credential__device__site")
            .filter(code=code)
            .first()
        )
        if row is None:
            raise NotFound("Invalid pairing code.")
        if row.used_at is not None:
            raise ValidationError({"code": "This pairing code was already used."})
        if row.expires_at <= timezone.now():
            raise ValidationError({"code": "This pairing code has expired."})

        cred = row.credential
        if not cred.is_active:
            raise ValidationError({"code": "Kiosk credential is inactive."})

        row.used_at = timezone.now()
        row.save(update_fields=["used_at"])

        device = cred.device
        return {
            "token": cred.token,
            "credential_id": cred.pk,
            "label": cred.label,
            "device_id": device.pk,
            "device_name": device.name,
            "site_id": device.site_id,
            "site_name": device.site.name,
            "nearpay_terminal_id": cred.nearpay_terminal_id,
        }
