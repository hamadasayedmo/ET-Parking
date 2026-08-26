"""Pay-at-exit payment intents (kiosk reports PSP result; platform opens gate)."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from parking.models import AccessRequest, Event, Session, SessionAuditEntry
from parking.services import pulse_device
from wallets.services import calculate_parking_fee

from .models import PaymentIntent
from .receipts import extract_card_last4_from_raw, normalize_card_last4

logger = logging.getLogger(__name__)

EXIT_PAYMENT_RETRY_REASON = "Exit payment issue — driver can retry"
OPEN_INTENT_STATUSES = (
    PaymentIntent.Status.CREATED,
    PaymentIntent.Status.PENDING,
)


def project_currency(project) -> str:
    return (getattr(project, "currency", None) or "SAR").upper()[:3]


def _paid_exit_deadline(session: Session):
    if session.paid_exit_until is not None:
        return session.paid_exit_until
    latest_paid_at = (
        session.payment_intents.filter(
            status=PaymentIntent.Status.PAID,
            paid_at__isnull=False,
        )
        .order_by("-paid_at")
        .values_list("paid_at", flat=True)
        .first()
    )
    if latest_paid_at is None:
        return None
    grace = session.site.effective_grace_minutes()
    return latest_paid_at + timedelta(minutes=grace)


def eligible_previous_paid_session(
    access_request: AccessRequest,
    *,
    for_update: bool = False,
) -> Session | None:
    """Latest same-plate/site session, when paid by card and outside grace."""
    if access_request.action != Event.Action.EXIT:
        return None
    qs = Session.objects.select_related("site", "zone", "entry_device").filter(
        plate=access_request.plate,
        site_id=access_request.device.site_id,
    )
    if for_update:
        qs = qs.select_for_update(of=("self",))
    session = qs.order_by("-start_time").first()
    if (
        session is None
        or session.billing_method != Session.BillingMethod.CARD
        or session.payment_status != Session.PaymentStatus.PAID
    ):
        return None
    deadline = _paid_exit_deadline(session)
    if deadline is None or deadline >= timezone.now():
        return None
    session.paid_exit_until = deadline
    return session


def _create_intent(
    *,
    access_request: AccessRequest,
    session: Session,
    amount: Decimal,
) -> PaymentIntent:
    project = access_request.device.site.project
    if amount <= Decimal("0.00"):
        raise ValueError("Calculated fee must be greater than zero.")
    return PaymentIntent.objects.create(
        project=project,
        session=session,
        access_request=access_request,
        amount=amount,
        currency=project_currency(project),
        merchant_reference_id=f"sess-{session.pk}-{uuid.uuid4().hex[:12]}",
        provider="",
        status=PaymentIntent.Status.PENDING,
    )


def create_payment_intent_for_access_request(
    access_request: AccessRequest,
) -> PaymentIntent:
    """
    Create (or reuse) a pending payment intent for an exit AccessRequest.
    Does not call any payment provider — the kiosk charges via its PSP.
    """
    access_request = AccessRequest.objects.select_related(
        "device",
        "device__site",
        "device__site__project",
    ).get(pk=access_request.pk)

    project = access_request.device.site.project
    if not project.pay_at_exit_enabled:
        raise ValueError("Pay-at-exit is not enabled for this project.")

    if access_request.status != AccessRequest.Status.PENDING:
        raise ValueError("Access request is not pending.")

    if access_request.action != Event.Action.EXIT:
        raise ValueError("Pay-at-exit payment is only for exit access requests.")

    from parking.services import device_has_active_kiosk

    if not device_has_active_kiosk(access_request.device):
        raise ValueError("No active kiosk paired to this exit — cannot send a bill.")

    existing = (
        PaymentIntent.objects.select_related("session")
        .filter(
            access_request=access_request,
            status__in=OPEN_INTENT_STATUSES,
        )
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        return existing

    session = Session.get_open(
        plate=access_request.plate,
        site_id=access_request.device.site_id,
    )
    if session is None:
        raise ValueError("No open session found for this plate at this site.")
    if session.payment_status == Session.PaymentStatus.PAID:
        raise ValueError(
            "Paid session requires a control-room extend/new-session choice."
        )

    if session.billing_method != Session.BillingMethod.CARD:
        session.billing_method = Session.BillingMethod.CARD
        session.save(update_fields=["billing_method"])

    return _create_intent(
        access_request=access_request,
        session=session,
        amount=session.estimated_fee(),
    )


def refresh_payment_intent_amount(access_request: AccessRequest) -> PaymentIntent:
    """
    Update an open kiosk bill to the current estimated fee for its session.
    Republishes via post_save SSE so the kiosk refreshes the amount.
    """
    with transaction.atomic():
        access_request = (
            AccessRequest.objects.select_for_update()
            .select_related("device", "device__site", "device__site__project")
            .get(pk=access_request.pk)
        )
        if access_request.status != AccessRequest.Status.PENDING:
            raise ValueError("Access request is not pending.")
        intent = (
            PaymentIntent.objects.select_for_update()
            .select_related("session")
            .filter(
                access_request=access_request,
                status__in=OPEN_INTENT_STATUSES,
            )
            .order_by("-created_at")
            .first()
        )
        if intent is None:
            raise ValueError("No open kiosk bill was found.")
        session = intent.session
        if session.payment_status == Session.PaymentStatus.PAID:
            raise ValueError("Paid session cannot refresh an open bill amount.")
        amount = session.estimated_fee()
        if amount <= Decimal("0.00"):
            raise ValueError("Calculated fee must be greater than zero.")
        if intent.amount == amount:
            return intent
        intent.amount = amount
        intent.save(update_fields=["amount", "updated_at"])
        return intent


def close_open_payment_intents_for_access_request(
    access_request: AccessRequest,
    *,
    status: str = PaymentIntent.Status.CANCELLED,
) -> int:
    """Close open bills one-by-one so the existing post_save SSE removes each."""
    if status not in (PaymentIntent.Status.FAILED, PaymentIntent.Status.CANCELLED):
        raise ValueError("Closed payment status must be failed or cancelled.")
    intents = list(
        PaymentIntent.objects.select_for_update()
        .filter(
            access_request=access_request,
            status__in=OPEN_INTENT_STATUSES,
        )
        .order_by("created_at")
    )
    for intent in intents:
        intent.status = status
        intent.save(update_fields=["status", "updated_at"])
    return len(intents)


def create_control_room_payment_intent(
    access_request: AccessRequest,
    *,
    mode: str,
    entry_time=None,
    actor=None,
) -> PaymentIntent:
    """Prepare an operator-selected bill while leaving the exit AR pending."""
    from parking.services import _ensure_session_for_exit_without_open

    with transaction.atomic():
        access_request = (
            AccessRequest.objects.select_for_update()
            .select_related("device", "device__site", "device__site__project")
            .get(pk=access_request.pk)
        )
        if access_request.status != AccessRequest.Status.PENDING:
            raise ValueError("Access request is not pending.")
        if access_request.action != Event.Action.EXIT:
            raise ValueError("Kiosk charging is only available for exit requests.")
        if not access_request.device.site.project.pay_at_exit_enabled:
            raise ValueError("Pay-at-exit is not enabled for this project.")

        existing = (
            PaymentIntent.objects.select_for_update()
            .filter(
                access_request=access_request,
                status__in=OPEN_INTENT_STATUSES,
            )
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing

        now = timezone.now()
        if mode == "extend_previous":
            session = eligible_previous_paid_session(
                access_request,
                for_update=True,
            )
            if session is None:
                raise ValueError(
                    "No eligible paid session outside grace can be extended."
                )
            deadline = _paid_exit_deadline(session)
            if deadline is None or deadline >= now:
                raise ValueError("The previous session is still within paid grace.")
            amount = calculate_parking_fee(
                deadline,
                now,
                rate_source=session.zone or session.site,
            )
            SessionAuditEntry.objects.create(
                session=session,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                action=SessionAuditEntry.Action.UPDATED,
                field="paid_exit_until",
                old_value=deadline.isoformat(),
                new_value="pending kiosk extension",
                note=(
                    f"Control room extended paid session for access request "
                    f"{access_request.pk}; incremental bill {amount}."
                ),
            )
        elif mode == "new_session":
            if entry_time is None:
                raise ValueError("Entry time is required for a new session.")
            if timezone.is_naive(entry_time):
                entry_time = timezone.make_aware(
                    entry_time,
                    timezone.get_current_timezone(),
                )
            if entry_time >= now:
                raise ValueError("Entry time must be before the current time.")

            old_sessions = list(
                Session.objects.select_for_update(of=("self",))
                .filter(
                    plate=access_request.plate,
                    site_id=access_request.device.site_id,
                    end_time__isnull=True,
                )
                .order_by("-start_time")
            )
            for old_session in old_sessions:
                if old_session.payment_status not in (
                    Session.PaymentStatus.PAID,
                    Session.PaymentStatus.EXEMPTED,
                ):
                    raise ValueError(
                        "An existing unpaid session must be resolved before "
                        "creating a new session."
                    )
                if entry_time < old_session.start_time:
                    raise ValueError(
                        "New entry time cannot be before the previous session started."
                    )
                old_session.end_time = entry_time
                old_session.save(update_fields=["end_time"])

            session = _ensure_session_for_exit_without_open(
                access_request,
                entry_time=entry_time,
            )
            session.billing_method = Session.BillingMethod.CARD
            session.payment_status = Session.PaymentStatus.PENDING
            session.fee = None
            session.paid_exit_until = None
            session.save(
                update_fields=[
                    "billing_method",
                    "payment_status",
                    "fee",
                    "paid_exit_until",
                ]
            )
            old_session_ids = [old_session.pk for old_session in old_sessions]
            for old_session in old_sessions:
                SessionAuditEntry.objects.create(
                    session=old_session,
                    actor=(
                        actor if getattr(actor, "is_authenticated", False) else None
                    ),
                    action=SessionAuditEntry.Action.ENDED,
                    field="end_time",
                    old_value="",
                    new_value=entry_time.isoformat(),
                    note=(
                        f"Administratively closed and split to new session "
                        f"{session.pk} for access request {access_request.pk}."
                    ),
                )
            amount = session.estimated_fee(now)
            SessionAuditEntry.objects.create(
                session=session,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                action=SessionAuditEntry.Action.UPDATED,
                field="start_time",
                old_value="",
                new_value=entry_time.isoformat(),
                note=(
                    "Control room created a new session for kiosk billing on "
                    f"access request {access_request.pk}; administratively split "
                    f"from session(s) {old_session_ids or 'none'}."
                ),
            )
        else:
            raise ValueError("mode must be extend_previous or new_session.")

        return _create_intent(
            access_request=access_request,
            session=session,
            amount=amount,
        )


def create_pay_at_exit_intent(access_request: AccessRequest) -> PaymentIntent | None:
    """
    Called when an exit AccessRequest becomes pending under pay-at-exit.
    Returns the intent, or None if creation is not applicable.
    Kiosk receives the bill via SSE (intent_bus), not WebSocket.
    """
    from parking.services import device_has_active_kiosk

    if not device_has_active_kiosk(access_request.device):
        logger.info(
            "Skip pay-at-exit intent for AR=%s: no active kiosk on device %s",
            access_request.pk,
            access_request.device_id,
        )
        return None
    try:
        return create_payment_intent_for_access_request(access_request)
    except ValueError as exc:
        logger.info(
            "Skip pay-at-exit intent for AR=%s: %s",
            access_request.pk,
            exc,
        )
        return None


# Back-compat alias for older imports / signals.
create_and_notify_pay_at_exit = create_pay_at_exit_intent


def complete_pay_at_exit_payment(intent: PaymentIntent) -> PaymentIntent:
    """
    Mark session paid, resolve AccessRequest, grant exit Event, pulse barrier.
    Idempotent if already paid.
    """
    with transaction.atomic():
        intent = (
            PaymentIntent.objects.select_for_update(of=("self",))
            .select_related(
                "session",
                "access_request",
                "access_request__device",
                "session__site",
            )
            .get(pk=intent.pk)
        )
        if intent.status == PaymentIntent.Status.PAID:
            intent.gate_opened = bool(
                intent.access_request
                and intent.access_request.status == AccessRequest.Status.APPROVED
            )
            return intent

        session = (
            Session.objects.select_for_update(of=("self",))
            .select_related("site")
            .get(pk=intent.session_id)
        )
        was_already_paid = session.payment_status == Session.PaymentStatus.PAID
        prior_fee = session.fee or Decimal("0.00")
        session.fee = prior_fee + intent.amount if was_already_paid else intent.amount
        session.payment_status = Session.PaymentStatus.PAID
        session.billing_method = Session.BillingMethod.CARD
        settled_at = timezone.now()
        session.paid_exit_until = settled_at + timedelta(
            minutes=session.site.effective_grace_minutes()
        )
        session.save(
            update_fields=[
                "fee",
                "payment_status",
                "billing_method",
                "paid_exit_until",
            ]
        )

        access_request = intent.access_request
        device_to_pulse = None
        if access_request is not None:
            access_request = (
                AccessRequest.objects.select_for_update()
                .select_related("device")
                .get(pk=access_request.pk)
            )
            if access_request.status == AccessRequest.Status.PENDING:
                event = Event.objects.create(
                    device=access_request.device,
                    plate=access_request.plate,
                    vehicle_type=access_request.vehicle_type,
                    action=Event.Action.EXIT,
                    decision=Event.Decision.GRANTED,
                )
                access_request.status = AccessRequest.Status.APPROVED
                access_request.decided_at = timezone.now()
                access_request.decision_note = (
                    access_request.decision_note or "Paid at exit kiosk"
                )
                access_request.resolution_event = event
                access_request.save(
                    update_fields=[
                        "status",
                        "decided_at",
                        "decision_note",
                        "resolution_event",
                    ]
                )
                device_to_pulse = access_request.device

        intent.status = PaymentIntent.Status.PAID
        intent.paid_at = settled_at
        intent.save(update_fields=["status", "paid_at", "updated_at"])
        intent.gate_opened = device_to_pulse is not None

    if device_to_pulse is not None:
        pulse_device(device_to_pulse)
    return intent


def _mark_access_request_retrying(intent: PaymentIntent) -> None:
    """Keep the bill/AR open while the driver retries another card."""
    if intent.access_request_id is None:
        return

    access_request = (
        AccessRequest.objects.select_for_update()
        .filter(
            pk=intent.access_request_id,
            status=AccessRequest.Status.PENDING,
        )
        .first()
    )
    if access_request is None:
        return

    access_request.reason = EXIT_PAYMENT_RETRY_REASON
    access_request.save(update_fields=["reason"])


def apply_payment_result(
    intent: PaymentIntent,
    *,
    status: str,
    provider: str = "",
    provider_transaction_id: str = "",
    raw_result: dict | None = None,
    card_last4: str = "",
) -> PaymentIntent:
    """Apply paid/failed result reported by the exit kiosk after its PSP charge."""
    status = (status or "").lower().strip()
    provider = (provider or "").strip().lower()[:32]
    with transaction.atomic():
        # Use the same AR → Session → Intent lock order as ops cancellation
        # and decisions so cancel-vs-paid races cannot deadlock.
        intent_refs = PaymentIntent.objects.only(
            "access_request_id",
            "session_id",
        ).get(pk=intent.pk)
        if intent_refs.access_request_id is not None:
            AccessRequest.objects.select_for_update().filter(
                pk=intent_refs.access_request_id
            ).first()
        Session.objects.select_for_update().filter(
            pk=intent_refs.session_id
        ).first()
        intent = PaymentIntent.objects.select_for_update().select_related(
            "session",
            "access_request",
        ).get(pk=intent.pk)
        if intent.status == PaymentIntent.Status.PAID:
            return intent

        if intent.status in (
            PaymentIntent.Status.FAILED,
            PaymentIntent.Status.CANCELLED,
        ):
            if status in ("failed", "fail", "cancelled", "canceled"):
                return intent

        if raw_result is not None:
            intent.raw_result = raw_result
        if provider:
            intent.provider = provider
        if provider_transaction_id:
            intent.provider_transaction_id = provider_transaction_id

        pin = normalize_card_last4(card_last4) or extract_card_last4_from_raw(
            raw_result if raw_result is not None else intent.raw_result
        )
        if pin:
            intent.pin = pin

        update_fields = [
            "raw_result",
            "provider",
            "provider_transaction_id",
            "updated_at",
        ]
        if pin:
            update_fields.append("pin")
        intent.save(update_fields=update_fields)

        if status in ("paid", "success", "successful"):
            return complete_pay_at_exit_payment(intent)

        if status in ("failed", "fail", "cancelled", "canceled"):
            # PSP decline/cancel is soft: keep the same bill open for retry.
            intent.status = PaymentIntent.Status.PENDING
            intent.save(update_fields=["status", "updated_at"])
            _mark_access_request_retrying(intent)
            return intent

        raise ValueError("status must be paid or failed.")


# Back-compat alias
apply_mobile_payment_result = apply_payment_result
