"""Card last-4 PIN + public receipt lookup (URL is built by the client/host)."""

from __future__ import annotations

import json
import re

from django.conf import settings

from payments.models import PaymentIntent
from wallets.services import normalize_plate

_LAST4_KEYS = frozenset(
    {
        "pansuffix",
        "pan_suffix",
        "last4",
        "last_4",
        "card_last4",
        "cardlast4",
    }
)
_PAN_KEYS = frozenset(
    {
        "pan",
        "masked_pan",
        "maskedpan",
        "card_number",
        "cardnumber",
    }
)


def normalize_card_last4(value: object) -> str:
    """
    Last 4 digits from a PAN / suffix.
    NearPay often sends masked PAN like "5239 68** **** 5774" with empty panSuffix.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.lower() in ("none", "null", "undefined"):
        return ""

    # Trailing digits after a mask block: **** 5774
    masked = re.search(r"\*{2,}\s*(\d{4})\s*$", s)
    if masked:
        return masked.group(1)

    # Plain "...5774" at end
    trailing = re.search(r"(\d{4})\s*$", s)
    if trailing and "*" in s:
        return trailing.group(1)

    digits = re.sub(r"\D", "", s)
    if len(digits) < 4:
        return ""
    return digits[-4:]


def extract_card_last4_from_raw(raw: object) -> str:
    """
    Best-effort last-4 from NearPay raw_result.

    Prefers non-empty panSuffix, then masked `pan` fields. Also parses
    stringified receipt.data blobs from NearPay events.
    """
    if raw is None:
        return ""

    suffix_hits: list[str] = []
    pan_hits: list[str] = []

    def walk(node: object, depth: int = 0) -> None:
        if depth > 10 or node is None:
            return

        if isinstance(node, str):
            text = node.strip()
            if len(text) >= 2 and text[0] in "{[":
                try:
                    walk(json.loads(text), depth + 1)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return
            return

        if isinstance(node, dict):
            for key, val in node.items():
                key_l = str(key).lower().replace("-", "_")
                if key_l in _LAST4_KEYS or key_l.endswith("pansuffix"):
                    pin = normalize_card_last4(val)
                    if pin:
                        suffix_hits.append(pin)
                elif key_l in _PAN_KEYS:
                    pin = normalize_card_last4(val)
                    if pin:
                        pan_hits.append(pin)
                walk(val, depth + 1)
            return

        if isinstance(node, list):
            for item in node[:50]:
                walk(item, depth + 1)

    walk(raw)
    if suffix_hits:
        return suffix_hits[0]
    if pan_hits:
        return pan_hits[0]
    return ""


def find_latest_receipt_intent(*, plate: str, pin: str) -> PaymentIntent | None:
    """
    Public lookup: only the plate's latest paid payment intent is valid,
    and only when its pin (card last 4) matches.
    """
    plate = normalize_plate(plate or "")
    pin = normalize_card_last4(pin)
    if not plate or not pin:
        return None

    latest = (
        PaymentIntent.objects.select_related(
            "session",
            "session__site",
            "project",
        )
        .filter(
            session__plate=plate,
            status=PaymentIntent.Status.PAID,
        )
        .order_by("-created_at")
        .first()
    )
    if latest is None:
        return None
    if normalize_card_last4(latest.pin) != pin:
        return None
    return latest


def receipt_payload(intent: PaymentIntent) -> dict:
    from payments.zatca import (
        DEFAULT_VAT_RATE,
        build_phase1_qr_base64,
        split_vat_inclusive,
    )

    session = intent.session
    project = intent.project
    powered = (getattr(settings, "KIOSK_POWERED_BY_NAME", "") or "").strip()

    country = (getattr(project, "country", None) or "SA").strip().upper() or "SA"
    seller_name = (getattr(project, "seller_name", None) or "").strip()
    vat_number = (getattr(project, "vat_number", None) or "").strip()

    invoice_ts = intent.paid_at or intent.created_at
    invoice_timestamp = invoice_ts.isoformat() if invoice_ts else ""

    net_amount = ""
    vat_amount = ""
    vat_rate = ""
    invoice_total = str(intent.amount)
    qr_base64 = ""

    if country == "SA":
        vat_rate = f"{DEFAULT_VAT_RATE:.2f}"
        net, vat, total = split_vat_inclusive(intent.amount, rate=DEFAULT_VAT_RATE)
        net_amount = f"{net:.2f}"
        vat_amount = f"{vat:.2f}"
        invoice_total = f"{total:.2f}"
        if seller_name and vat_number and invoice_timestamp:
            qr_base64 = build_phase1_qr_base64(
                seller_name=seller_name,
                vat_number=vat_number,
                invoice_timestamp=invoice_timestamp,
                invoice_total_with_vat=total,
                vat_amount=vat,
            )

    return {
        "plate": session.plate,
        "amount": str(intent.amount),
        "currency": intent.currency,
        "site_name": session.site.name,
        "session_start": session.start_time,
        "session_end": session.end_time,
        "paid_at": intent.paid_at,
        "powered_by_name": powered,
        "country": country,
        "seller_name": seller_name,
        "vat_number": vat_number,
        "vat_rate": vat_rate,
        "invoice_timestamp": invoice_timestamp,
        "invoice_total_with_vat": invoice_total,
        "vat_amount": vat_amount,
        "net_amount": net_amount,
        "invoice_number": intent.merchant_reference_id,
        "qr_base64": qr_base64,
    }

