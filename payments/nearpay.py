"""NearPay Terminal SDK JWT minting (RS256 with pos_key.pem)."""

from __future__ import annotations

from pathlib import Path

import jwt
from django.conf import settings


class NearPayConfigError(ValueError):
    """Missing or invalid NearPay signing configuration."""


def _private_key_pem() -> bytes:
    path = (getattr(settings, "NEARPAY_PRIVATE_KEY_PATH", "") or "").strip()
    if not path:
        raise NearPayConfigError(
            "NEARPAY_PRIVATE_KEY_PATH is not set (path to pos_key.pem)."
        )
    key_path = Path(path)
    if not key_path.is_file():
        raise NearPayConfigError(f"NearPay private key not found: {key_path}")
    return key_path.read_bytes()


def build_nearpay_payload(
    *,
    terminal_id: str,
    client_uuid: str | None = None,
    merchant_uuid: str | None = None,
) -> dict:
    tid = (terminal_id or "").strip()
    client = (client_uuid or getattr(settings, "NEARPAY_CLIENT_UUID", "") or "").strip()
    merchant = (
        merchant_uuid or getattr(settings, "NEARPAY_MERCHANT_UUID", "") or ""
    ).strip()

    if not tid:
        raise NearPayConfigError(
            "terminal_id is required (set nearpay_terminal_id on the kiosk credential)."
        )
    if bool(client) == bool(merchant):
        raise NearPayConfigError(
            "Set exactly one of NEARPAY_CLIENT_UUID or NEARPAY_MERCHANT_UUID "
            "(or pass one in the request)."
        )

    data: dict[str, str] = {"ops": "auth", "terminal_id": tid}
    if client:
        data["client_uuid"] = client
    else:
        data["merchant_uuid"] = merchant
    return {"data": data}


def mint_nearpay_jwt(
    *,
    terminal_id: str,
    client_uuid: str | None = None,
    merchant_uuid: str | None = None,
) -> str:
    """Sign a NearPay auth JWT with pos_key.pem (RS256)."""
    payload = build_nearpay_payload(
        terminal_id=terminal_id,
        client_uuid=client_uuid,
        merchant_uuid=merchant_uuid,
    )
    return jwt.encode(payload, _private_key_pem(), algorithm="RS256")
