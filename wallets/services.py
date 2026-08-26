from decimal import Decimal

from django.db import transaction

from .models import Vehicle, Wallet


def normalize_plate(plate_str: str) -> str:
    """Uppercase and remove all whitespace"""
    return "".join(str(plate_str).split()).upper()


def verify_active_subscription(plate_str: str) -> bool:
    cleaned_plate = normalize_plate(plate_str)

    vehicle = (
        Vehicle.objects.select_related("wallet")
        .filter(plate=cleaned_plate, is_active=True)
        .first()
    )

    return vehicle is not None and vehicle.wallet.is_active


def calculate_parking_fee(start_time, end_time, *, site=None, rate_source=None) -> Decimal:
    """
    Ceil hours (min 1) × hourly_rate + additional_fee.

    Prefer rate_source (Zone or Site). `site=` kept for backward compatibility.
    """
    if not end_time:
        return Decimal("0.00")
    source = rate_source if rate_source is not None else site
    if source is None:
        return Decimal("0.00")
    duration = end_time - start_time
    hours = max(1, int((duration.total_seconds() + 3599) // 3600))
    return (Decimal(hours) * source.hourly_rate) + source.additional_fee


def minimum_entry_balance(site) -> Decimal:
    """Funds required to enter (covers the first billed hour + additional fee)."""
    return site.minimum_entry_balance()


def get_wallet_for_plate(plate_str: str) -> Wallet | None:
    """Return wallet for an active subscribed plate, or None."""
    cleaned_plate = normalize_plate(plate_str)
    vehicle = (
        Vehicle.objects.select_related("wallet")
        .filter(plate=cleaned_plate, is_active=True)
        .first()
    )
    if vehicle is None or not vehicle.wallet.is_active:
        return None
    return vehicle.wallet


def get_operator_wallet(user) -> Wallet | None:
    """Return the active operator cash wallet assigned to this staff user."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    wallet = (
        Wallet.objects.select_related("project", "assigned_user")
        .filter(assigned_user_id=user.pk, is_active=True)
        .first()
    )
    return wallet


def plate_wallet_inactive_reason(plate_str: str) -> str | None:
    """
    If the plate is linked to a wallet/vehicle that is inactive, return a
    control-room reason string. None when there is no plate match at all.
    """
    cleaned_plate = normalize_plate(plate_str)
    vehicle = (
        Vehicle.objects.select_related("wallet")
        .filter(plate=cleaned_plate)
        .order_by("-is_active", "-id")
        .first()
    )
    if vehicle is None:
        return None
    if not vehicle.is_active and not vehicle.wallet.is_active:
        return (
            f"Vehicle and wallet are inactive "
            f"({vehicle.wallet.name})"
        )
    if not vehicle.is_active:
        return f"Vehicle is inactive (wallet: {vehicle.wallet.name})"
    if not vehicle.wallet.is_active:
        return f"Wallet is inactive ({vehicle.wallet.name})"
    return None


# Back-compat alias used by older call sites during rename.
get_client_wallet = get_wallet_for_plate


def plate_wallet_is_exempted(plate_str: str) -> bool:
    """True when the plate belongs to an active, billing-exempt wallet."""
    cleaned_plate = normalize_plate(plate_str)
    return Vehicle.objects.filter(
        plate=cleaned_plate,
        is_active=True,
        wallet__is_active=True,
        wallet__is_exempted=True,
    ).exists()


plate_client_is_exempted = plate_wallet_is_exempted


@transaction.atomic
def sync_wallet_session_exemption(wallet: Wallet) -> int:
    """
    Apply wallet exemption to open (and pending-wallet) sessions owned by this wallet.

    Prefer Session.wallet (billing owner). Also include legacy rows that only match
    by current plate ownership when wallet is unset.
    """
    from django.db.models import Q

    from parking.models import Session

    plates = list(wallet.vehicles.values_list("plate", flat=True))
    owned = Q(wallet=wallet)
    if plates:
        owned |= Q(wallet__isnull=True, plate__in=plates)

    open_sessions = Session.objects.filter(owned, end_time__isnull=True)
    if wallet.is_exempted:
        updated = open_sessions.update(
            billing_exempt=True, allow_negative_balance=False
        )
        pending = list(
            Session.objects.select_for_update().filter(
                owned,
                end_time__isnull=False,
                payment_status=Session.PaymentStatus.PENDING,
                billing_method=Session.BillingMethod.WALLET,
            )
        )
        for session in pending:
            fee = session.estimated_fee(session.end_time)
            session.billing_exempt = True
            session.allow_negative_balance = False
            session.fee = fee
            session.payment_status = Session.PaymentStatus.EXEMPTED
            session.save(
                update_fields=[
                    "billing_exempt",
                    "allow_negative_balance",
                    "fee",
                    "payment_status",
                ]
            )
        return updated

    return open_sessions.update(
        billing_exempt=False, allow_negative_balance=True
    )


sync_client_session_exemption = sync_wallet_session_exemption
