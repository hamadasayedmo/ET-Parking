"""Cashier hub: zone assignments, on-site plate search, desk validate."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import Cashier
from parking.models import Session, SessionAuditEntry
from parking.services import (
    MATCH_SUGGESTION_LIMIT,
    MATCH_SUGGESTION_MIN_PERCENT,
    MATCH_SUGGESTION_STRONG_PERCENT,
    _charge_operator_wallet_for_session,
    _paid_exit_grace_until,
    _residual_fee_after_paid_grace,
    plate_match_percent,
)
from wallets.services import get_operator_wallet, normalize_plate


def cashier_zone_ids(user) -> list[int]:
    """Zone PKs assigned to this user as cashier (empty if none)."""
    if not getattr(user, "is_authenticated", False):
        return []
    return list(
        Cashier.objects.filter(user_id=user.pk).values_list("zone_id", flat=True)
    )


def user_is_cashier_for_zone(user, zone_id: int | None) -> bool:
    if zone_id is None:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return Cashier.objects.filter(user_id=user.pk, zone_id=zone_id).exists()


def cashier_hub_context(user) -> dict:
    """Zones + sites for the cashier hub shell."""
    qs = (
        Cashier.objects.filter(user_id=user.pk)
        .select_related("zone", "zone__site", "zone__site__project")
        .order_by("zone__site__name", "zone__name")
    )
    if getattr(user, "is_superuser", False) and not qs.exists():
        # Superusers without assignments can still open the hub empty —
        # admin should assign zones; we don't dump every zone here.
        pass

    zones = []
    site_ids: set[int] = set()
    for row in qs:
        zone = row.zone
        site = zone.site
        site_ids.add(site.pk)
        zones.append(
            {
                "id": zone.pk,
                "name": zone.name,
                "site_id": site.pk,
                "site_name": site.name,
                "project_id": site.project_id,
                "project_name": site.project.name,
            }
        )
    return {
        "zones": zones,
        "zone_ids": [z["id"] for z in zones],
        "site_ids": sorted(site_ids),
        "has_assignment": bool(zones),
    }


def search_on_site_sessions_for_cashier(
    user,
    *,
    query: str,
    limit: int = MATCH_SUGGESTION_LIMIT,
) -> list[dict]:
    """
    Rank open sessions in the cashier's zones by plate similarity.

    Exact matches (100%) first, then nearest plates using the same
    ``plate_match_percent`` algorithm as exit OCR matching (≥50%).
    """
    cleaned = normalize_plate(query or "")
    if len(cleaned) < 2:
        return []

    zone_ids = cashier_zone_ids(user)
    if not zone_ids and not getattr(user, "is_superuser", False):
        return []

    sessions = (
        Session.objects.filter(end_time__isnull=True)
        .select_related("site", "zone", "wallet", "site__project")
        .order_by("-start_time")
    )
    if zone_ids:
        sessions = sessions.filter(zone_id__in=zone_ids)
    elif not getattr(user, "is_superuser", False):
        return []

    scored: list[tuple[int, bool, Session]] = []
    for session in sessions[:500]:
        pct = plate_match_percent(cleaned, session.plate)
        if pct < MATCH_SUGGESTION_MIN_PERCENT:
            continue
        exact = pct == 100
        scored.append((pct, exact, session))

    scored.sort(key=lambda row: (-row[0], -int(row[1]), row[2].start_time, row[2].pk))
    now = timezone.now()
    results = []
    for pct, exact, session in scored[: max(1, min(limit, 10))]:
        fee = session.estimated_fee(now)
        paid_grace = (
            session.payment_status == Session.PaymentStatus.PAID
            and session.within_paid_exit_grace(now)
        )
        results.append(
            {
                "session_id": session.pk,
                "plate": session.plate,
                "match_percent": pct,
                "exact": exact,
                "weak": pct < MATCH_SUGGESTION_STRONG_PERCENT,
                "site_id": session.site_id,
                "site_name": session.site.name,
                "zone_id": session.zone_id,
                "zone_name": session.zone.name if session.zone_id else None,
                "start_time": session.start_time,
                "payment_status": session.payment_status,
                "billing_method": session.billing_method,
                "fee": str(fee),
                "paid_exit_until": session.paid_exit_until,
                "within_paid_exit_grace": paid_grace,
                "can_validate": (
                    not paid_grace
                    and session.billing_method != Session.BillingMethod.CARD
                    and session.payment_status
                    != Session.PaymentStatus.EXEMPTED
                ),
            }
        )
    return results


def validate_open_session_for_exit(
    session: Session,
    user,
    *,
    note: str = "",
) -> Session:
    """
    Desk validate: charge the operator wallet, mark the open stay PAID with
    paid-exit grace, keep the session open so the car can leave at the gate
    within the grace window.
    """
    note = (note or "").strip() or "Validated at cashier desk"
    with transaction.atomic():
        session = (
            Session.objects.select_for_update(of=("self",))
            .select_related("site", "zone", "wallet")
            .get(pk=session.pk)
        )
        if session.end_time is not None:
            raise ValidationError(
                {"detail": "Session is already closed — use settle instead."}
            )
        if session.billing_method == Session.BillingMethod.CARD:
            raise ValidationError(
                {"detail": "Card sessions settle via kiosk / pay-at-exit."}
            )
        if session.payment_status == Session.PaymentStatus.EXEMPTED:
            raise ValidationError(
                {"detail": "Session is waived — clear exemption before validating."}
            )
        if not user_is_cashier_for_zone(user, session.zone_id):
            raise ValidationError(
                {"detail": "You are not assigned as cashier for this zone."}
            )

        operator_wallet = get_operator_wallet(user)
        if operator_wallet is None:
            raise ValidationError(
                {
                    "detail": (
                        "Assign an operator wallet to your user before "
                        "validating payment."
                    )
                }
            )

        now = timezone.now()
        if (
            session.payment_status == Session.PaymentStatus.PAID
            and session.within_paid_exit_grace(now)
        ):
            return session

        if (
            session.payment_status == Session.PaymentStatus.PAID
            and session.paid_exit_until
            and not session.within_paid_exit_grace(now)
        ):
            fee = Decimal(str(_residual_fee_after_paid_grace(session, at=now))).quantize(
                Decimal("0.01")
            )
            residual = True
        else:
            fee = Decimal(str(session.estimated_fee(now))).quantize(Decimal("0.01"))
            residual = False

        _charge_operator_wallet_for_session(
            operator_wallet=operator_wallet,
            session=session,
            amount=fee,
            user=user,
            reference=f"cashier-validate-session-{session.pk}-{int(now.timestamp())}",
        )

        grace_until = _paid_exit_grace_until(session, at=now)
        if residual:
            session.fee = (session.fee or Decimal("0.00")) + fee
            session.paid_exit_until = grace_until
            session.billing_method = Session.BillingMethod.WALLET
            session.save(
                update_fields=["fee", "paid_exit_until", "billing_method"]
            )
            SessionAuditEntry.objects.create(
                session=session,
                actor=user,
                action=SessionAuditEntry.Action.NOTE,
                field="payment_status",
                old_value="paid",
                new_value="cashier_validate_residual",
                note=f"{note} · residual {fee} charged to operator wallet",
            )
        else:
            session.fee = fee
            session.billing_exempt = False
            session.allow_negative_balance = True
            session.payment_status = Session.PaymentStatus.PAID
            session.billing_method = Session.BillingMethod.WALLET
            session.waiver_kind = ""
            session.paid_exit_until = grace_until
            session.save(
                update_fields=[
                    "fee",
                    "billing_exempt",
                    "allow_negative_balance",
                    "payment_status",
                    "billing_method",
                    "waiver_kind",
                    "paid_exit_until",
                ]
            )
            SessionAuditEntry.objects.create(
                session=session,
                actor=user,
                action=SessionAuditEntry.Action.NOTE,
                field="payment_status",
                old_value="pending",
                new_value="cashier_validate",
                note=(
                    f"{note} · {fee} charged to operator wallet "
                    f"#{operator_wallet.pk} · exit grace until {grace_until.isoformat()}"
                ),
            )

    session.refresh_from_db()
    return session
