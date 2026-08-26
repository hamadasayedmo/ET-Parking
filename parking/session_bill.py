"""ZATCA Phase-1 compliant printable tax invoice for a parking session."""

from __future__ import annotations

import html
import io
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from parking.models import Session
from payments.models import PaymentIntent
from payments.zatca import (
    DEFAULT_VAT_RATE,
    build_phase1_qr_base64,
    split_vat_inclusive,
)


def _esc(value) -> str:
    if value is None:
        return "—"
    return html.escape(str(value))


def _dt(value) -> str:
    """Short local timestamp — receipt width leaves no room for offsets."""
    if value is None:
        return "—"
    return timezone.localtime(value).strftime("%d/%m/%y %H:%M")


def _money(value: Decimal | str | int | float | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _qr_svg_markup(payload: str, *, size: int = 128) -> str:
    """Render Base64 TLV payload as an inline SVG QR (no Pillow required)."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    factory = SvgPathImage
    # Small box/border keeps the printed QR compact on a 80mm roll.
    img = qrcode.make(
        payload,
        image_factory=factory,
        box_size=3,
        border=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    stream = io.BytesIO()
    img.save(stream)
    svg = stream.getvalue().decode("utf-8")
    # Force a fixed display size for print layout.
    if "<svg" in svg and "width=" not in svg.split(">", 1)[0]:
        svg = svg.replace("<svg", f'<svg width="{size}" height="{size}"', 1)
    return svg


def session_bill_amount(session: Session) -> Decimal:
    if session.fee is not None:
        return _money(session.fee)
    return _money(session.estimated_fee())


def latest_paid_intent_for_session(session: Session) -> PaymentIntent | None:
    return (
        PaymentIntent.objects.filter(
            session_id=session.pk,
            status=PaymentIntent.Status.PAID,
        )
        .order_by("-paid_at", "-created_at")
        .first()
    )


def session_can_download_bill(session: Session) -> bool:
    """Bills exist only for successful card payments made at the exit kiosk."""
    return (
        session.payment_status == Session.PaymentStatus.PAID
        and session.billing_method == Session.BillingMethod.CARD
        and latest_paid_intent_for_session(session) is not None
    )


def session_bill_payload(session: Session) -> dict:
    """Flat ZATCA-aligned payload (same field names as public receipts)."""
    session = (
        Session.objects.select_related(
            "site",
            "site__project",
            "zone",
            "wallet",
        ).get(pk=session.pk)
    )
    project = session.site.project
    intent = latest_paid_intent_for_session(session)
    if (
        session.payment_status != Session.PaymentStatus.PAID
        or session.billing_method != Session.BillingMethod.CARD
        or intent is None
    ):
        raise ValueError(
            "A bill is available only after a successful pay-at-exit card payment."
        )

    country = (getattr(project, "country", None) or "SA").strip().upper() or "SA"
    seller_name = (getattr(project, "seller_name", None) or "").strip()
    vat_number = (getattr(project, "vat_number", None) or "").strip()
    currency = (
        (intent.currency if intent else None)
        or getattr(project, "currency", None)
        or "SAR"
    )
    powered = (getattr(settings, "KIOSK_POWERED_BY_NAME", "") or "").strip()
    if not powered:
        powered = (getattr(project, "powered_by_name", None) or "").strip()

    amount = session_bill_amount(session)
    invoice_ts = None
    if intent is not None:
        invoice_ts = intent.paid_at or intent.created_at
    if invoice_ts is None:
        invoice_ts = session.end_time or timezone.now()
    invoice_timestamp = invoice_ts.isoformat() if invoice_ts else ""

    invoice_number = (
        intent.merchant_reference_id
        if intent is not None and intent.merchant_reference_id
        else f"session-{session.pk}"
    )

    net_amount = ""
    vat_amount = ""
    vat_rate = ""
    invoice_total = f"{amount:.2f}"
    qr_base64 = ""

    if country == "SA":
        vat_rate = f"{DEFAULT_VAT_RATE:.2f}"
        net, vat, total = split_vat_inclusive(amount, rate=DEFAULT_VAT_RATE)
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
        "session_id": session.pk,
        "plate": session.plate,
        "amount": f"{amount:.2f}",
        "currency": currency,
        "site_name": session.site.name,
        "zone_name": session.zone.name if session.zone_id else "",
        "session_start": session.start_time,
        "session_end": session.end_time,
        "payment_status": session.payment_status,
        "billing_method": session.billing_method,
        "paid_at": intent.paid_at if intent else None,
        "powered_by_name": powered,
        "country": country,
        "seller_name": seller_name,
        "vat_number": vat_number,
        "vat_rate": vat_rate,
        "invoice_timestamp": invoice_timestamp,
        "invoice_total_with_vat": invoice_total,
        "vat_amount": vat_amount,
        "net_amount": net_amount,
        "invoice_number": invoice_number,
        "qr_base64": qr_base64,
        "card_last4": (intent.pin if intent else "") or "",
    }


def build_session_bill_html(session: Session, *, generated_by: str = "") -> str:
    """Self-contained printable tax invoice HTML with ZATCA Phase-1 QR."""
    data = session_bill_payload(session)
    brand = data["seller_name"] or data["powered_by_name"] or "Parking"
    has_vat = bool(data["vat_amount"] and data["net_amount"] and data["country"] == "SA")
    qr_block = ""
    if data["qr_base64"]:
        try:
            qr_block = (
                f'<div class="qr">{_qr_svg_markup(data["qr_base64"], size=128)}</div>'
                '<p class="hint">Scan to verify (ZATCA)</p>'
            )
        except Exception:
            # Fallback: still expose the TLV payload for external QR tools.
            b64 = html.escape(data["qr_base64"])
            qr_block = (
                '<div class="qr missing">QR unavailable</div>'
                f'<p class="hint mono break">{b64}</p>'
            )
    else:
        qr_block = (
            '<div class="qr missing">QR unavailable</div>'
            '<p class="hint">Set seller name + VAT number</p>'
        )

    duration = "—"
    if data["session_start"] and data["session_end"]:
        seconds = int((data["session_end"] - data["session_start"]).total_seconds())
        hours, rem = divmod(max(seconds, 0), 3600)
        minutes = rem // 60
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    card = f" ···· {data['card_last4']}" if data["card_last4"] else ""

    vat_rows = ""
    if has_vat:
        vat_rows = (
            f'<div class="row"><span>Net</span>'
            f'<span>{_esc(data["net_amount"])} {_esc(data["currency"])}</span></div>'
            f'<div class="row"><span>VAT {_esc(data["vat_rate"])}%</span>'
            f'<span>{_esc(data["vat_amount"])} {_esc(data["currency"])}</span></div>'
        )

    generated = _dt(timezone.now())
    invoice_dt = data["invoice_timestamp"]
    if invoice_dt:
        parsed = parse_datetime(invoice_dt)
        invoice_dt = _dt(parsed) if parsed else invoice_dt.replace("T", " ")[:16]
    else:
        invoice_dt = "—"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Simplified tax invoice · {_esc(data['plate'])} · {_esc(data['invoice_number'])}</title>
  <style>
    :root {{ color-scheme: light; }}
    /* 80mm thermal roll, auto height so the page cuts right after content. */
    @page {{ size: 80mm auto; margin: 0; }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Segoe UI", system-ui, sans-serif;
      margin: 0;
      padding: 6px;
      background: #f4f4f5;
      color: #000;
      font-size: 11px;
      line-height: 1.25;
    }}
    .sheet {{
      width: 72mm;
      margin: 0 auto;
      background: #fff;
      padding: 4mm 3mm;
    }}
    .center {{ text-align: center; }}
    .brand {{
      font-size: 14px;
      font-weight: 700;
      margin: 0;
      line-height: 1.2;
    }}
    .kicker {{
      font-size: 9px;
      letter-spacing: .12em;
      text-transform: uppercase;
      margin: 0 0 1px;
    }}
    .vat {{ font-size: 10px; margin: 1px 0 0; }}
    .plate {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 16px;
      letter-spacing: .12em;
      font-weight: 700;
      margin: 3px 0 0;
    }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    hr {{
      border: 0;
      border-top: 1px dashed #000;
      margin: 4px 0;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 6px;
      padding: 1px 0;
    }}
    .row span:first-child {{ flex: 0 0 auto; }}
    .row span:last-child {{ text-align: right; word-break: break-word; }}
    .total {{
      font-size: 14px;
      font-weight: 700;
      padding: 2px 0;
    }}
    .qr {{ margin: 4px auto 0; width: 34mm; }}
    .qr svg {{ width: 100%; height: auto; display: block; }}
    .qr.missing {{
      border: 1px dashed #000;
      padding: 6px;
      font-size: 9px;
      text-align: center;
    }}
    .hint {{ font-size: 8px; margin: 2px 0 0; line-height: 1.2; }}
    .break {{ word-break: break-all; }}
    .foot {{ font-size: 8px; margin: 4px 0 0; line-height: 1.25; }}
    @media print {{
      body {{ background: #fff; padding: 0; font-size: 10px; }}
      .sheet {{ width: auto; padding: 1mm 2mm; }}
      .no-print {{ display: none !important; }}
    }}
  </style>
</head>
<body>
  <div class="sheet">
    <p class="no-print" style="margin:0 0 4px;font-size:9px">
      Print or save as PDF (80mm roll).
    </p>

    <div class="center">
      <p class="kicker">Simplified tax invoice</p>
      <p class="kicker" dir="rtl" lang="ar">فاتورة ضريبية مبسطة</p>
      <p class="brand">{_esc(brand)}</p>
      {f'<p class="vat">VAT <span class="mono">{_esc(data["vat_number"])}</span></p>' if data["vat_number"] else ""}
      <p class="plate">{_esc(data["plate"])}</p>
    </div>

    <hr />

    <div class="row"><span>Site</span><span>{_esc(data["site_name"])}{(' · ' + _esc(data["zone_name"])) if data["zone_name"] else ''}</span></div>
    <div class="row"><span>Invoice</span><span class="mono">{_esc(data["invoice_number"])}</span></div>
    <div class="row"><span>Date</span><span>{_esc(invoice_dt)}</span></div>
    <div class="row"><span>Entry</span><span>{_esc(_dt(data["session_start"]))}</span></div>
    <div class="row"><span>Exit</span><span>{_esc(_dt(data["session_end"]))}</span></div>
    <div class="row"><span>Duration</span><span>{_esc(duration)}</span></div>
    <div class="row"><span>Payment</span><span>Card{_esc(card)}</span></div>

    <hr />

    {vat_rows}
    <div class="row total"><span>Total</span><span>{_esc(data["invoice_total_with_vat"])} {_esc(data["currency"])}</span></div>

    <hr />

    <div class="center">
      {qr_block}
    </div>

    <p class="foot center">
      Session #{_esc(data["session_id"])}{" · " + _esc(generated_by) if generated_by else ""} · {_esc(generated)}{(" · Powered by " + _esc(data["powered_by_name"])) if data["powered_by_name"] and data["powered_by_name"] != data["seller_name"] else ""}
    </p>
  </div>
</body>
</html>
"""
