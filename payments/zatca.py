"""ZATCA Phase-1 simplified e-invoice QR (TLV tags 1–5 → Base64)."""

from __future__ import annotations

import base64
from decimal import Decimal, ROUND_HALF_UP


DEFAULT_VAT_RATE = Decimal("15")


def _money(value: Decimal | str | int | float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def split_vat_inclusive(
    total: Decimal | str | int | float,
    *,
    rate: Decimal | str | int | float = DEFAULT_VAT_RATE,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Split a VAT-inclusive total into (net, vat, total).

    vat = round(total * rate / (100 + rate), 2)
    net = total - vat
    """
    total_d = _money(total)
    rate_d = Decimal(str(rate))
    if rate_d <= 0:
        return total_d, Decimal("0.00"), total_d
    vat = _money(total_d * rate_d / (Decimal("100") + rate_d))
    net = _money(total_d - vat)
    return net, vat, total_d


def tlv_encode(tag: int, value: str) -> bytes:
    encoded = (value or "").encode("utf-8")
    if len(encoded) > 255:
        raise ValueError(f"TLV value for tag {tag} exceeds 255 bytes")
    return bytes([tag, len(encoded)]) + encoded


def build_phase1_qr_base64(
    *,
    seller_name: str,
    vat_number: str,
    invoice_timestamp: str,
    invoice_total_with_vat: Decimal | str,
    vat_amount: Decimal | str,
) -> str:
    """
    Build ZATCA Phase-1 QR payload (tags 1–5) as Base64.

    Tag 1: seller name
    Tag 2: VAT registration number
    Tag 3: invoice timestamp (ISO-8601)
    Tag 4: invoice total with VAT
    Tag 5: VAT amount
    """
    total_s = f"{_money(invoice_total_with_vat):.2f}"
    vat_s = f"{_money(vat_amount):.2f}"
    payload = b"".join(
        [
            tlv_encode(1, seller_name),
            tlv_encode(2, vat_number),
            tlv_encode(3, invoice_timestamp),
            tlv_encode(4, total_s),
            tlv_encode(5, vat_s),
        ]
    )
    return base64.b64encode(payload).decode("ascii")
