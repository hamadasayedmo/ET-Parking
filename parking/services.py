from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from parking.models import AccessRequest, Event, Session, SessionAuditEntry
from parking.mqtt import publish_once
from wallets.services import (
    get_operator_wallet,
    get_wallet_for_plate,
    normalize_plate,
    plate_wallet_inactive_reason,
    plate_wallet_is_exempted,
)

MANUAL_GATE_CONTROL_REASON = "Manual entry from gate control"


@dataclass
class AuthorizeResult:
    """Auto-grant yields an Event; auto-deny escalates to a pending AccessRequest."""

    event: Event | None = None
    access_request: AccessRequest | None = None

    @property
    def granted(self) -> bool:
        return (
            self.event is not None
            and self.event.decision == Event.Decision.GRANTED
        )


def verify_wallet_access(plate_str: str, *, device=None) -> bool:
    """
    Classic prepaid check: active vehicle wallet + enough balance.

    Exempted wallets may enter without a minimum balance, and may exit
    (with or without an open session) without wallet checks.
    Exempted open sessions may exit without wallet checks.
    Same-gate re-entry and grace exits auto-grant.
    """
    cleaned_plate = normalize_plate(plate_str)
    device_type = getattr(device, "type", None) if device is not None else None
    wallet_exempt = plate_wallet_is_exempted(cleaned_plate)

    if device_type == "exit":
        if wallet_exempt:
            return True
        open_sessions = Session.get_open_stack(
            plate=cleaned_plate, site_id=device.site_id
        )
        if not open_sessions:
            return False

        from parking.zone_sessions import has_open_child, session_slice_fee

        target = None
        zone = getattr(device, "zone", None)
        if zone is not None:
            for session in open_sessions:
                if session.zone_id == zone.pk:
                    target = session
                    break
        if target is None:
            target = open_sessions[0]

        if has_open_child(target, open_sessions):
            return False

        if target.within_nested_grace() or target.within_visit_grace():
            return True
        if target.billing_exempt:
            return True
        # Desk / operator validate: already paid with exit grace — let them out.
        if (
            target.payment_status == Session.PaymentStatus.PAID
            and target.within_paid_exit_grace()
        ):
            return True
        if target.billing_method == Session.BillingMethod.CARD:
            return False

        wallet = get_wallet_for_plate(cleaned_plate)
        if wallet is None:
            return False

        # Innermost with outers: need cover for full stack.
        outers = []
        if target.parent_session_id:
            from parking.zone_sessions import collect_outers

            outers = collect_outers(target, open_sessions)
        if outers:
            from decimal import Decimal
            from django.utils import timezone

            at = timezone.now()
            total = sum(
                (session_slice_fee(s, at_time=at) for s in [target, *outers]),
                Decimal("0.00"),
            )
            return wallet.balance >= total

        return target.wallet_can_cover(wallet)

    if wallet_exempt:
        return True

    # Same-gate reopen: already inside — no new balance check.
    open_session = Session.get_open(plate=cleaned_plate, site_id=device.site_id)
    if (
        open_session is not None
        and open_session.entry_device_id == getattr(device, "pk", None)
    ):
        return True

    wallet = get_wallet_for_plate(cleaned_plate)
    if wallet is None:
        return False

    zone = getattr(device, "zone", None) if device is not None else None
    if zone is not None:
        return wallet.balance >= zone.minimum_entry_balance()

    site = device.site if device is not None else None
    if site is None:
        return False
    return wallet.balance >= site.minimum_entry_balance()


def verify_plate_access(plate_str: str, *, device=None) -> bool:
    """Resolve auto-access using project wallet / pay-at-exit flags."""
    granted, _method = resolve_access(plate_str, device=device)
    return granted


def resolve_access(
    plate_str: str, *, device=None
) -> tuple[bool, str | None]:
    """
    Returns (granted, billing_method_if_granting).

    Combinations:
    - wallet on, pay_at_exit off → wallet only
    - wallet off, pay_at_exit on → card entry always; exit holds (unless exempt)
    - both on → wallet first, else pay-at-exit
    - both off → never auto-grant
    - exempt client → auto-grant entry/exit when any auto mode is enabled
    """
    if device is None:
        return False, None

    project = device.site.project
    wallet_on = project.wallet_enabled
    pay_at_exit_on = project.pay_at_exit_enabled

    if not wallet_on and not pay_at_exit_on:
        return False, None

    cleaned_plate = normalize_plate(plate_str)
    if plate_wallet_is_exempted(cleaned_plate):
        # Prefer wallet billing metadata when wallet mode is on; otherwise card.
        method = (
            Session.BillingMethod.WALLET
            if wallet_on
            else Session.BillingMethod.CARD
        )
        return True, method

    if wallet_on and verify_wallet_access(cleaned_plate, device=device):
        return True, Session.BillingMethod.WALLET

    if pay_at_exit_on:
        device_type = getattr(device, "type", None)
        if device_type == "entry":
            return True, Session.BillingMethod.CARD
        latest_session = (
            Session.objects.select_related("site")
            .filter(
                plate=cleaned_plate,
                site_id=device.site_id,
                payment_status=Session.PaymentStatus.PAID,
                paid_exit_until__isnull=False,
            )
            .order_by("-start_time")
            .first()
        )
        if latest_session and latest_session.within_paid_exit_grace():
            return True, latest_session.billing_method or Session.BillingMethod.CARD
        # Exit: hold for kiosk pay-at-exit / AccessRequest
        return False, None

    # Wallet-only: paid exit grace (operator validate / settle) allows auto re-exit.
    latest_paid = (
        Session.objects.select_related("site")
        .filter(
            plate=cleaned_plate,
            site_id=device.site_id,
            payment_status=Session.PaymentStatus.PAID,
            paid_exit_until__isnull=False,
        )
        .order_by("-start_time")
        .first()
    )
    if latest_paid and latest_paid.within_paid_exit_grace():
        return True, latest_paid.billing_method or Session.BillingMethod.WALLET

    return False, None


def access_denial_reason(plate_str: str, *, device) -> str:
    """
    Human-readable reason for creating a pending AccessRequest.
    Call only when resolve_access has already denied auto-grant.
    """
    cleaned_plate = normalize_plate(plate_str)
    action = _device_action(device)
    project = device.site.project
    wallet_on = project.wallet_enabled
    pay_at_exit_on = project.pay_at_exit_enabled

    if not wallet_on and not pay_at_exit_on:
        return "Manual mode — auto-open is disabled for this project"

    if action == Event.Action.EXIT:
        open_session = Session.get_open(plate=cleaned_plate, site_id=device.site_id)
        if open_session is None:
            latest_paid = (
                Session.objects.filter(
                    plate=cleaned_plate,
                    site_id=device.site_id,
                    payment_status=Session.PaymentStatus.PAID,
                    paid_exit_until__isnull=False,
                )
                .order_by("-start_time")
                .first()
            )
            if latest_paid is not None and not latest_paid.within_paid_exit_grace():
                if latest_paid.billing_method == Session.BillingMethod.CARD:
                    return "Paid session outside grace — control-room billing required"
                return (
                    "Wallet / fleet payment outside grace — remaining fee due"
                )
            if latest_paid is not None:
                return "Paid session outside grace — control-room billing required"
            return "No open entry session for this plate at this site"
        if open_session.payment_status == Session.PaymentStatus.PAID:
            if open_session.billing_method == Session.BillingMethod.WALLET:
                return (
                    "Wallet / fleet payment outside grace — remaining fee due"
                )
            return "Paid session outside grace — control-room billing required"
        if open_session.billing_method == Session.BillingMethod.CARD:
            if pay_at_exit_on:
                if not device_has_active_kiosk(device):
                    return "No kiosk paired — decide manually"
                return "Awaiting exit payment"
            return "Card session cannot auto-open on wallet rules — awaiting control room"
        if pay_at_exit_on and not wallet_on:
            if not device_has_active_kiosk(device):
                return "No kiosk paired — decide manually"
            return "Awaiting exit payment"
        wallet = get_wallet_for_plate(cleaned_plate)
        if wallet is None:
            inactive = plate_wallet_inactive_reason(cleaned_plate)
            if inactive:
                return inactive
            if pay_at_exit_on:
                if not device_has_active_kiosk(device):
                    return "No kiosk paired — decide manually"
                return "Awaiting exit payment"
            return "No active wallet linked to this plate"
        if not open_session.wallet_can_cover(wallet):
            if pay_at_exit_on:
                if not device_has_active_kiosk(device):
                    return "No kiosk paired — decide manually"
                return (
                    f"Pay at kiosk — wallet balance insufficient "
                    f"({wallet.balance} {wallet.currency}) for estimated exit fee"
                )
            return (
                f"Insufficient wallet balance "
                f"({wallet.balance} {wallet.currency}) for estimated exit fee"
            )
        return "Auto-access denied — awaiting control room"

    # Entry
    if pay_at_exit_on and not wallet_on:
        # Should normally auto-grant; keep a fallback message.
        return "Auto-access denied — awaiting control room"
    wallet = get_wallet_for_plate(cleaned_plate)
    if wallet is None:
        inactive = plate_wallet_inactive_reason(cleaned_plate)
        if inactive:
            return inactive
        return "No active subscription / wallet for this plate"
    site = device.site
    minimum = site.minimum_entry_balance()
    if wallet.balance < minimum:
        return (
            f"Insufficient wallet balance for entry "
            f"({wallet.balance} {wallet.currency}; need at least {minimum})"
        )
    return "Auto-access denied — awaiting control room"


def _device_action(device) -> str:
    action = device.type
    if action not in (Event.Action.ENTRY, Event.Action.EXIT):
        return Event.Action.ENTRY
    return action


def normalize_vehicle_type(value) -> str:
    """Optional ANPR vehicle type; empty when absent (old gateways)."""
    if value is None:
        return ""
    return str(value).strip()[:32]


def device_has_active_kiosk(device) -> bool:
    """True when this exit has an active kiosk credential (can show a bill)."""
    if getattr(device, "type", None) != "exit":
        return False
    # Query DB — do not use the reverse OneToOne cache (stale after delete/pair).
    from payments.models import KioskCredential

    return KioskCredential.objects.filter(
        device_id=device.pk, is_active=True
    ).exists()


def plate_edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two normalized plates."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def plate_match_percent(a: str, b: str) -> int:
    """
    Plate similarity as a percent of the longer plate.

    Identical plates → 100%. Uses edit distance so a one-character flip on a
    6-char plate is ~83%, not a raw distance count.
    """
    left = normalize_plate(a)
    right = normalize_plate(b)
    longer = max(len(left), len(right))
    if longer == 0:
        return 100
    distance = plate_edit_distance(left, right)
    return max(0, int(round((1 - (distance / longer)) * 100)))


OCR_FLIP_MAX_DISTANCE = 3
# Operator match suggestions: hide noise below this, mark borderline as weak.
MATCH_SUGGESTION_MIN_PERCENT = 50
MATCH_SUGGESTION_STRONG_PERCENT = 60
MATCH_SUGGESTION_LIMIT = 3


_EXIT_MATCH_REASON_RE = re.compile(
    r"Exit OCR (?P<ocr>\S+) matched by operator to open session "
    r"#(?P<session_id>\d+)"
)
EXIT_MATCH_AUDIT_FIELDS = (
    SessionAuditEntry.EXIT_MATCH_FIELD,
    SessionAuditEntry.EXIT_UNMATCH_FIELD,
)


def parse_exit_match_reason(reason: str) -> tuple[str, int] | None:
    match = _EXIT_MATCH_REASON_RE.search(reason or "")
    if not match:
        return None
    return match.group("ocr"), int(match.group("session_id"))


def latest_exit_match_audit(session_id: int) -> SessionAuditEntry | None:
    return (
        SessionAuditEntry.objects.filter(
            session_id=session_id,
            field__in=EXIT_MATCH_AUDIT_FIELDS,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def session_has_active_exit_match(session_id: int) -> bool:
    latest = latest_exit_match_audit(session_id)
    return (
        latest is not None
        and latest.field == SessionAuditEntry.EXIT_MATCH_FIELD
    )


def find_exit_match_audit(
    *,
    session_id: int | None = None,
    access_request_id: int | None = None,
) -> SessionAuditEntry | None:
    """Latest match audit that is still active (not undone)."""
    if session_id is not None:
        latest = latest_exit_match_audit(session_id)
        if latest is None or latest.field != SessionAuditEntry.EXIT_MATCH_FIELD:
            return None
        if access_request_id is not None and (
            f"access request #{access_request_id}" not in (latest.note or "")
        ):
            return None
        return latest

    if access_request_id is None:
        return None

    entry = (
        SessionAuditEntry.objects.filter(
            field=SessionAuditEntry.EXIT_MATCH_FIELD,
            note__contains=f"access request #{access_request_id}",
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if entry is None:
        return None
    latest = latest_exit_match_audit(entry.session_id)
    if latest is None or latest.field != SessionAuditEntry.EXIT_MATCH_FIELD:
        return None
    if f"access request #{access_request_id}" not in (latest.note or ""):
        return None
    return latest


def find_pending_ocr_match(device, plate: str) -> AccessRequest | None:
    """
    Pending AR on this device whose plate is the same or within OCR flip distance.

    Used so a noisy re-read (ABC123 → ABC12B) keeps the waiting request instead of
    deleting it and starting over.
    """
    cleaned = normalize_plate(plate)
    pending = list(
        AccessRequest.objects.select_for_update()
        .filter(device=device, status=AccessRequest.Status.PENDING)
        .order_by("created_at")
    )
    if not pending:
        return None
    best: AccessRequest | None = None
    best_dist: int | None = None
    for access_request in pending:
        dist = plate_edit_distance(normalize_plate(access_request.plate), cleaned)
        if dist == 0:
            return access_request
        if dist <= OCR_FLIP_MAX_DISTANCE and (
            best_dist is None or dist < best_dist
        ):
            best = access_request
            best_dist = dist
    return best


def session_matches_for_access_request(
    access_request: AccessRequest,
) -> list[dict]:
    """Open sessions in the exit zone, strongest plate match first.

    Only returns suggestions at or above MATCH_SUGGESTION_MIN_PERCENT so
    unrelated plates (e.g. 14%) never appear as normal candidates.
    """
    if (
        access_request.action != Event.Action.EXIT
        or access_request.status != AccessRequest.Status.PENDING
    ):
        return []

    zone_id = access_request.device.zone_id
    sessions = list(
        Session.objects.select_related("entry_device", "zone")
        .filter(
            site_id=access_request.device.site_id,
            zone_id=zone_id,
            end_time__isnull=True,
        )
        .order_by("start_time")
    )
    parent_ids = {
        session.parent_session_id
        for session in sessions
        if session.parent_session_id is not None
    }
    rows = []
    for session in sessions:
        if session.pk in parent_ids:
            continue
        match_percent = plate_match_percent(
            access_request.plate,
            session.plate,
        )
        if match_percent < MATCH_SUGGESTION_MIN_PERCENT:
            continue
        rows.append(
            {
                "id": session.pk,
                "plate": session.plate,
                "match_percent": match_percent,
                "weak": match_percent < MATCH_SUGGESTION_STRONG_PERCENT,
                "start_time": session.start_time,
                "entry_device_label": str(session.entry_device),
                "zone_name": session.zone.name if session.zone_id else None,
            }
        )
    rows.sort(
        key=lambda row: (-row["match_percent"], row["start_time"], row["id"])
    )
    return rows[:MATCH_SUGGESTION_LIMIT]


def match_access_request_to_session(
    access_request: AccessRequest,
    session_id: int,
    user,
) -> AccessRequest:
    """
    Link a no-entry exit read to an existing open stay.

    The session plate remains canonical. The observed exit OCR is retained in
    the AR reason and the operator match is stamped on the session audit log.
    """
    with transaction.atomic():
        access_request = (
            AccessRequest.objects.select_for_update()
            .select_related(
                "device",
                "device__zone",
                "device__site",
                "device__site__project",
            )
            .get(pk=access_request.pk)
        )
        if access_request.status != AccessRequest.Status.PENDING:
            raise ValueError("Only a pending access request can be matched.")
        if access_request.action != Event.Action.EXIT:
            raise ValueError("Only an exit access request can be matched.")
        if Session.get_open(
            plate=access_request.plate,
            site_id=access_request.device.site_id,
        ) is not None:
            raise ValueError("This exit already has an open session.")

        session = (
            Session.objects.select_for_update()
            .select_related("entry_device", "zone")
            .filter(
                pk=session_id,
                site_id=access_request.device.site_id,
                zone_id=access_request.device.zone_id,
                end_time__isnull=True,
            )
            .first()
        )
        if session is None:
            raise ValueError("Selected session is not open in this exit zone.")
        if Session.objects.filter(
            parent_session_id=session.pk,
            end_time__isnull=True,
        ).exists():
            raise ValueError("Selected session still has an open child zone.")
        if session_has_active_exit_match(session.pk):
            raise ValueError("Selected session already has an active exit match.")

        observed_plate = access_request.plate
        match_percent = plate_match_percent(observed_plate, session.plate)
        access_request.plate = session.plate
        access_request.reason = (
            f"Exit OCR {observed_plate} matched by operator to open session "
            f"#{session.pk} ({session.plate}, match {match_percent}%)"
        )
        access_request.save(update_fields=["plate", "reason"])
        SessionAuditEntry.objects.create(
            session=session,
            actor=user,
            action=SessionAuditEntry.Action.NOTE,
            field=SessionAuditEntry.EXIT_MATCH_FIELD,
            old_value=observed_plate,
            new_value=session.plate,
            note=f"Matched exit access request #{access_request.pk}",
        )

        if access_request.device.site.project.pay_at_exit_enabled:
            from payments.services import create_pay_at_exit_intent

            create_pay_at_exit_intent(access_request)

    return access_request


def unmatch_access_request_from_session(
    access_request: AccessRequest,
    user,
) -> AccessRequest:
    """
    Undo an operator exit↔session match.

    Keeps the original match audit and appends an undo audit entry.
    Only allowed while the access request is still pending.
    """
    with transaction.atomic():
        access_request = (
            AccessRequest.objects.select_for_update()
            .select_related(
                "device",
                "device__zone",
                "device__site",
                "device__site__project",
            )
            .get(pk=access_request.pk)
        )
        if access_request.status != AccessRequest.Status.PENDING:
            raise ValueError("Only a pending access request can be unmatched.")
        if access_request.action != Event.Action.EXIT:
            raise ValueError("Only an exit access request can be unmatched.")

        parsed = parse_exit_match_reason(access_request.reason)
        match_audit = find_exit_match_audit(access_request_id=access_request.pk)
        if parsed is None and match_audit is None:
            raise ValueError("This access request is not linked to a matched session.")

        ocr_plate = (
            parsed[0]
            if parsed is not None
            else (match_audit.old_value if match_audit else "")
        )
        session_id = (
            parsed[1]
            if parsed is not None
            else (match_audit.session_id if match_audit else None)
        )
        ocr_plate = normalize_plate(ocr_plate)
        if not ocr_plate or session_id is None:
            raise ValueError("Could not restore the original exit OCR plate.")

        session = (
            Session.objects.select_for_update()
            .filter(pk=session_id, end_time__isnull=True)
            .first()
        )
        if session is None:
            raise ValueError("Matched session is no longer open.")
        if not session_has_active_exit_match(session.pk):
            raise ValueError("This session match was already undone.")

        from payments.services import close_open_payment_intents_for_access_request

        close_open_payment_intents_for_access_request(access_request)

        access_request.plate = ocr_plate
        access_request.reason = access_denial_reason(
            ocr_plate, device=access_request.device
        )
        access_request.save(update_fields=["plate", "reason"])

        SessionAuditEntry.objects.create(
            session=session,
            actor=user,
            action=SessionAuditEntry.Action.NOTE,
            field=SessionAuditEntry.EXIT_UNMATCH_FIELD,
            old_value=session.plate,
            new_value=ocr_plate,
            note=f"Undid match of exit access request #{access_request.pk}",
        )

    return access_request


def unmatch_session_exit(session: Session, user) -> Session:
    """Undo the active exit match on a session.

    Works whether or not the linked exit access request is still pending.
    Pending ARs are restored to the original OCR plate; decided ARs are left
    as-is and only the session match audit is undone.
    """
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=session.pk)
        if session.end_time is not None:
            raise ValueError("Cannot unmatch a closed session.")
        if not session_has_active_exit_match(session.pk):
            raise ValueError("This session has no active exit match.")

        match_audit = find_exit_match_audit(session_id=session.pk)
        if match_audit is None:
            raise ValueError("This session has no active exit match.")

        access_request_id = None
        note_match = re.search(r"access request #(\d+)", match_audit.note or "")
        if note_match:
            access_request_id = int(note_match.group(1))

        access_request = None
        if access_request_id is not None:
            access_request = (
                AccessRequest.objects.select_for_update()
                .filter(pk=access_request_id, action=Event.Action.EXIT)
                .first()
            )

        if (
            access_request is not None
            and access_request.status == AccessRequest.Status.PENDING
        ):
            unmatch_access_request_from_session(access_request, user)
        else:
            from payments.services import close_open_payment_intents_for_access_request

            if access_request is not None:
                close_open_payment_intents_for_access_request(access_request)

            ocr_plate = normalize_plate(match_audit.old_value or "")
            SessionAuditEntry.objects.create(
                session=session,
                actor=user,
                action=SessionAuditEntry.Action.NOTE,
                field=SessionAuditEntry.EXIT_UNMATCH_FIELD,
                old_value=session.plate,
                new_value=ocr_plate or (match_audit.old_value or ""),
                note=(
                    f"Undid match of exit access request #{access_request_id}"
                    if access_request_id is not None
                    else "Undid exit match"
                ),
            )

        session.refresh_from_db()
    return session


def timeout_pending_access_requests(device) -> list[AccessRequest]:
    """
    Drop any pending access request on this device so at most one car waits.

    Timed-out requests are deleted (not kept as status=timeout): they are not
    operator decisions, clutter history/timelines, and Decisions already hides
    them. Any leftover historical timeout rows on the same device are purged
    in the same pass. Gate Events are not written for timeouts.
    """
    timed_out: list[AccessRequest] = []
    with transaction.atomic():
        requests = list(
            AccessRequest.objects.select_for_update()
            .filter(device=device, status=AccessRequest.Status.PENDING)
            .order_by("created_at")
        )
        for access_request in requests:
            from payments.services import close_open_payment_intents_for_access_request

            close_open_payment_intents_for_access_request(access_request)
            timed_out.append(access_request)
            access_request.delete()

        # Clear prior timeout rows left from older builds / sims.
        AccessRequest.objects.filter(
            device=device,
            status=AccessRequest.Status.TIMEOUT,
        ).delete()
    return timed_out


def purge_timeout_access_requests(*, plate: str, site_id: int) -> int:
    """Remove timed-out ARs for a plate at a site (e.g. after a real decision)."""
    deleted, _ = AccessRequest.objects.filter(
        plate=normalize_plate(plate),
        device__site_id=site_id,
        status=AccessRequest.Status.TIMEOUT,
    ).delete()
    return deleted


def authorize_plate(
    device,
    plate: str,
    *,
    vehicle_type: str | None = None,
) -> AuthorizeResult:
    from parking.billing_context import set_pending_billing_method

    cleaned_plate = normalize_plate(plate)
    cleaned_vehicle_type = normalize_vehicle_type(vehicle_type)
    action = _device_action(device)

    if not device.site.is_active:
        event = Event.objects.create(
            device=device,
            plate=cleaned_plate,
            vehicle_type=cleaned_vehicle_type,
            action=action,
            decision=Event.Decision.DENIED,
        )
        return AuthorizeResult(event=event)

    has_access, billing_method = resolve_access(cleaned_plate, device=device)

    with transaction.atomic():
        # Same plate or OCR flip (≤3 edits) of a waiting AR → keep that AR.
        ocr_match = find_pending_ocr_match(device, cleaned_plate)
        if ocr_match is not None:
            if normalize_plate(ocr_match.plate) != cleaned_plate:
                marker = f"OCR also read {cleaned_plate}"
                if marker not in (ocr_match.reason or ""):
                    ocr_match.reason = (
                        f"{ocr_match.reason} · {marker}"
                        if ocr_match.reason
                        else marker
                    )
                    ocr_match.save(update_fields=["reason"])
            return AuthorizeResult(access_request=ocr_match)

        timeout_pending_access_requests(device)

        if has_access:
            set_pending_billing_method(billing_method)
            event = Event.objects.create(
                device=device,
                plate=cleaned_plate,
                vehicle_type=cleaned_vehicle_type,
                action=action,
                decision=Event.Decision.GRANTED,
            )
            return AuthorizeResult(event=event)

        access_request = AccessRequest.objects.create(
            device=device,
            plate=cleaned_plate,
            vehicle_type=cleaned_vehicle_type,
            action=action,
            status=AccessRequest.Status.PENDING,
            reason=access_denial_reason(cleaned_plate, device=device),
        )
        return AuthorizeResult(access_request=access_request)


def staff_process_plate(
    device,
    plate: str,
    user,
    *,
    note: str = "",
    exempted: bool = False,
    entry_time: datetime | str | None = None,
) -> AccessRequest:
    """
    Gate-control manual plate entry: create an AccessRequest and auto-approve
    it (grant Event + pulse). Does not use camera auto-access / pay-at-exit hold.
    """
    cleaned_plate = normalize_plate(plate)
    if not cleaned_plate:
        raise ValueError("Plate is required.")

    if not device.enabled:
        raise ValueError("Device is disabled.")

    if not device.site.is_active:
        raise ValueError("Site is inactive.")

    if AccessRequest.objects.filter(
        device=device,
        status=AccessRequest.Status.PENDING,
    ).exists():
        raise ValueError("A vehicle is already waiting at this gate.")

    action = _device_action(device)
    decision_note = (note or "").strip() or MANUAL_GATE_CONTROL_REASON
    access_request = AccessRequest.objects.create(
        device=device,
        plate=cleaned_plate,
        action=action,
        status=AccessRequest.Status.PENDING,
        reason=MANUAL_GATE_CONTROL_REASON,
    )

    return approve_access_request(
        access_request,
        user,
        note=decision_note,
        exempted=exempted,
        entry_time=entry_time,
    )


def pulse_device(device, *, client=None) -> bool:
    """Open the barrier via MQTT pulse. Prefer an existing client when given."""
    topic = device.get_command_topic()
    payload = device.get_command_payload("pulse")
    if client is not None:
        from parking.mqtt import publish_on_client

        return publish_on_client(client, topic, payload)
    return publish_once(topic, payload)


def _coerce_entry_time(entry_time: datetime | str | None) -> datetime | None:
    if entry_time is None or entry_time == "":
        return None
    if isinstance(entry_time, datetime):
        dt = entry_time
    else:
        raw = str(entry_time).strip()
        dt = parse_datetime(raw)
        if dt is None:
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError("Invalid entry time.") from exc
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _ensure_session_for_exit_without_open(
    access_request: AccessRequest,
    *,
    entry_time: datetime,
) -> Session:
    """
    Create a synthetic open session from an operator-provided entry time so the
    upcoming exit grant Event can close it and trigger fee calculation.
    """
    from sites.models import Device

    site = access_request.device.site
    entry_device = (
        Device.objects.filter(
            site_id=site.pk,
            type=Device.Type.ENTRY,
            enabled=True,
        )
        .order_by("id")
        .first()
        or access_request.device
    )

    # Creating a granted entry Event lets the normal signal open the Session.
    Event.objects.create(
        device=entry_device,
        plate=access_request.plate,
        action=Event.Action.ENTRY,
        decision=Event.Decision.GRANTED,
    )
    session = Session.get_open(
        plate=access_request.plate,
        site_id=site.pk,
    )
    if session is None:
        raise ValueError("Failed to create session from entry time.")

    session.start_time = entry_time
    session.billing_exempt = plate_wallet_is_exempted(access_request.plate)
    session.allow_negative_balance = not session.billing_exempt
    # Control-room synthesized sessions settle via wallet path unless project
    # is pay-at-exit-only (no wallet).
    project = site.project
    if project.pay_at_exit_enabled and not project.wallet_enabled:
        session.billing_method = Session.BillingMethod.CARD
    else:
        session.billing_method = Session.BillingMethod.WALLET
    session.save(
        update_fields=[
            "start_time",
            "billing_exempt",
            "allow_negative_balance",
            "billing_method",
        ]
    )
    return session


def access_request_requires_entry_time(access_request: AccessRequest) -> bool:
    """
    True when approving an exit with no open session needs an operator entry time
    so a fee can be calculated and charged.

    Wallet-only projects with no wallet for the plate skip this: entry time would
    only create a session that fails to charge.
    """
    if access_request.action != Event.Action.EXIT:
        return False
    if (
        Session.get_open(
            plate=access_request.plate,
            site_id=access_request.device.site_id,
        )
        is not None
    ):
        return False
    if latest_unpaid_wallet_session(
        access_request.plate, access_request.device.site_id
    ):
        return False
    project = access_request.device.site.project
    if project.wallet_enabled and not project.pay_at_exit_enabled:
        if get_wallet_for_plate(access_request.plate) is None:
            return False
    return True


def latest_unpaid_wallet_session(plate: str, site_id: int) -> Session | None:
    """Latest closed wallet session still awaiting settlement."""
    return (
        Session.objects.filter(
            plate=normalize_plate(plate),
            site_id=site_id,
            end_time__isnull=False,
            payment_status=Session.PaymentStatus.PENDING,
            billing_method=Session.BillingMethod.WALLET,
        )
        .order_by("-end_time")
        .first()
    )


def escalate_unsettled_wallet_session(session: Session, *, wallet, fee) -> AccessRequest | None:
    """
    Raise a control-room AR when a closed wallet session could not be charged.
    Sessions are never marked payment-failed.
    """
    device = None
    if session.end_event_id:
        device = session.end_event.device
    if device is None:
        device = session.entry_device
    if device is None:
        return None

    if AccessRequest.objects.filter(
        device=device,
        plate=session.plate,
        status=AccessRequest.Status.PENDING,
    ).exists():
        return None

    reason = (
        f"Wallet could not cover parking fee "
        f"(balance {wallet.balance} {wallet.currency}; fee {fee})"
    )
    return AccessRequest.objects.create(
        device=device,
        plate=session.plate,
        vehicle_type="",
        action=Event.Action.EXIT,
        status=AccessRequest.Status.PENDING,
        reason=reason,
    )


def approve_access_request(
    access_request: AccessRequest,
    user,
    *,
    note: str = "",
    exempted: bool = False,
    entry_time: datetime | str | None = None,
) -> AccessRequest:
    """
    Control-room approve: grant Event, pulse barrier, resolve request.

    exempted=True → session billed as EXEMPTED (no wallet withdrawal).
    exempted=False → session may drive wallet balance negative on charge.

    For exit with no open session and not exempted, entry_time is required when
    a billable wallet session can be created (see access_request_requires_entry_time).
    """
    parsed_entry_time = _coerce_entry_time(entry_time)
    wallet_exempt = plate_wallet_is_exempted(access_request.plate)
    exempted = bool(exempted) or wallet_exempt
    note = (note or "").strip()

    with transaction.atomic():
        access_request = (
            AccessRequest.objects.select_for_update()
            .select_related(
                "device",
                "device__site",
                "device__site__project",
            )
            .get(pk=access_request.pk)
        )
        if access_request.status != AccessRequest.Status.PENDING:
            raise ValueError(
                f"Access request {access_request.pk} is not pending "
                f"(status={access_request.status})"
            )

        # Pay-at-exit control-room approve = open without kiosk charge → waive.
        project = access_request.device.site.project
        if (
            access_request.action == Event.Action.EXIT
            and project.pay_at_exit_enabled
        ):
            exempted = True

        if exempted and not note:
            raise ValueError(
                "Operator note is required when approving without billing / waiving."
            )

        # For exit, mark/create the open session before the grant Event closes it
        # so charge_wallet_on_session_end sees the billing flags.
        if access_request.action == Event.Action.EXIT:
            open_session = Session.get_open(
                plate=access_request.plate,
                site_id=access_request.device.site_id,
            )
            if open_session is not None:
                open_session.billing_exempt = exempted
                open_session.allow_negative_balance = not exempted
                update_fields = ["billing_exempt", "allow_negative_balance"]
                if exempted:
                    open_session.waiver_kind = Session.WaiverKind.OPERATOR
                    update_fields.append("waiver_kind")
                elif open_session.waiver_kind:
                    open_session.waiver_kind = ""
                    update_fields.append("waiver_kind")
                open_session.save(update_fields=update_fields)
            else:
                unpaid = latest_unpaid_wallet_session(
                    access_request.plate, access_request.device.site_id
                )
                if unpaid is not None:
                    unpaid.billing_exempt = exempted
                    unpaid.allow_negative_balance = not exempted
                    update_fields = ["billing_exempt", "allow_negative_balance"]
                    if exempted:
                        unpaid.payment_status = Session.PaymentStatus.EXEMPTED
                        unpaid.waiver_kind = Session.WaiverKind.OPERATOR
                        update_fields.extend(["payment_status", "waiver_kind"])
                    unpaid.save(update_fields=update_fields)
                elif not exempted and access_request_requires_entry_time(
                    access_request
                ):
                    if parsed_entry_time is None:
                        raise ValueError(
                            "Entry time is required to approve an exit with no "
                            "open session (or mark as exempted)."
                        )
                    now = timezone.now()
                    if parsed_entry_time >= now:
                        raise ValueError("Entry time must be before the exit time.")
                    _ensure_session_for_exit_without_open(
                        access_request,
                        entry_time=parsed_entry_time,
                    )

        from payments.services import close_open_payment_intents_for_access_request

        close_open_payment_intents_for_access_request(access_request)

        event = Event.objects.create(
            device=access_request.device,
            plate=access_request.plate,
            vehicle_type=access_request.vehicle_type,
            action=access_request.action,
            decision=Event.Decision.GRANTED,
        )
        access_request.status = AccessRequest.Status.APPROVED
        access_request.decided_by = user
        access_request.decided_at = timezone.now()
        access_request.decision_note = note
        access_request.exempted = exempted
        access_request.resolution_event = event
        access_request.save(
            update_fields=[
                "status",
                "decided_by",
                "decided_at",
                "decision_note",
                "exempted",
                "resolution_event",
            ]
        )

        # For entry, the grant Event just created the session — stamp flags now.
        if access_request.action == Event.Action.ENTRY:
            open_session = Session.get_open(
                plate=access_request.plate,
                site_id=access_request.device.site_id,
            )
            if open_session is not None:
                project = access_request.device.site.project
                if project.pay_at_exit_enabled and not project.wallet_enabled:
                    open_session.billing_method = Session.BillingMethod.CARD
                open_session.billing_exempt = exempted
                open_session.allow_negative_balance = not exempted
                update_fields = [
                    "billing_method",
                    "billing_exempt",
                    "allow_negative_balance",
                ]
                if exempted:
                    open_session.waiver_kind = Session.WaiverKind.OPERATOR
                    update_fields.append("waiver_kind")
                elif open_session.waiver_kind:
                    open_session.waiver_kind = ""
                    update_fields.append("waiver_kind")
                open_session.save(update_fields=update_fields)

    purge_timeout_access_requests(
        plate=access_request.plate,
        site_id=access_request.device.site_id,
    )
    pulse_device(access_request.device)
    return access_request


def _paid_exit_grace_until(session: Session, *, at=None):
    minutes = session.site.effective_grace_minutes()
    base = at or timezone.now()
    if minutes <= 0:
        return base
    return base + timedelta(minutes=minutes)


def _residual_fee_after_paid_grace(session: Session, *, at=None):
    from wallets.services import calculate_parking_fee

    end = at or timezone.now()
    start = session.paid_exit_until or session.end_time or session.start_time
    rate_source = session.zone or session.site
    return calculate_parking_fee(start, end, rate_source=rate_source)


def _charge_operator_wallet_for_session(
    *,
    operator_wallet,
    session: Session,
    amount,
    user,
    reference: str,
):
    from decimal import Decimal

    from wallets.models import Transaction

    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amount <= 0:
        return
    ok = operator_wallet.withdraw(
        amount,
        session=session,
        reference=reference,
        transaction_type=Transaction.Type.OPERATOR_VALIDATE,
        allow_negative=True,
        created_by=user,
    )
    if not ok:
        raise ValueError("Could not charge the operator wallet.")


def validate_payment_access_request(
    access_request: AccessRequest,
    user,
    *,
    note: str = "",
) -> AccessRequest:
    """
    Operator cash validation: charge the operator wallet (negative OK), mark the
    stay paid with paid-exit grace, open the gate.
    """
    from decimal import Decimal

    note = (note or "").strip() or "Validated payment via operator wallet"
    with transaction.atomic():
        access_request = (
            AccessRequest.objects.select_for_update()
            .select_related(
                "device",
                "device__site",
                "device__site__project",
                "device__zone",
            )
            .get(pk=access_request.pk)
        )
        if access_request.status != AccessRequest.Status.PENDING:
            raise ValueError("Access request is not pending.")
        if access_request.action != Event.Action.EXIT:
            raise ValueError("Only exit access requests can validate payment.")
        project = access_request.device.site.project
        if project.pay_at_exit_enabled and not project.wallet_enabled:
            raise ValueError(
                "Use the kiosk / pay-at-exit path for this project."
            )

        operator_wallet = get_operator_wallet(user)
        if operator_wallet is None:
            raise ValueError(
                "Assign an operator wallet to your user before validating payment."
            )

        now = timezone.now()
        open_session = Session.get_open(
            plate=access_request.plate,
            site_id=access_request.device.site_id,
        )
        residual_session = None
        if open_session is None:
            residual_session = (
                Session.objects.select_for_update(of=("self",))
                .select_related("site", "zone", "wallet")
                .filter(
                    plate=access_request.plate,
                    site_id=access_request.device.site_id,
                    payment_status=Session.PaymentStatus.PAID,
                    paid_exit_until__isnull=False,
                )
                .order_by("-start_time")
                .first()
            )
            if residual_session is None or residual_session.within_paid_exit_grace(now):
                raise ValueError(
                    "No billable open session (or residual after grace) to validate."
                )
            fee = _residual_fee_after_paid_grace(residual_session, at=now)
            session = residual_session
        else:
            session = (
                Session.objects.select_for_update(of=("self",))
                .select_related("site", "zone", "wallet")
                .get(pk=open_session.pk)
            )
            fee = session.estimated_fee(now)

        fee = Decimal(str(fee)).quantize(Decimal("0.01"))
        _charge_operator_wallet_for_session(
            operator_wallet=operator_wallet,
            session=session,
            amount=fee,
            user=user,
            reference=f"operator-validate-ar-{access_request.pk}-{int(now.timestamp())}",
        )

        grace_until = _paid_exit_grace_until(session, at=now)
        if residual_session is not None:
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
                new_value="operator_validate_residual",
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
                new_value="operator_validate",
                note=f"{note} · {fee} charged to operator wallet #{operator_wallet.pk}",
            )

        from payments.services import close_open_payment_intents_for_access_request

        close_open_payment_intents_for_access_request(access_request)

        event = Event.objects.create(
            device=access_request.device,
            plate=access_request.plate,
            vehicle_type=access_request.vehicle_type,
            action=Event.Action.EXIT,
            decision=Event.Decision.GRANTED,
        )
        access_request.status = AccessRequest.Status.APPROVED
        access_request.decided_by = user
        access_request.decided_at = now
        access_request.decision_note = note
        access_request.exempted = False
        access_request.resolution_event = event
        access_request.reason = (
            access_request.reason
            if "Wallet / fleet payment" in (access_request.reason or "")
            else (
                f"{access_request.reason} · Wallet / fleet payment validated"
                if access_request.reason
                else "Wallet / fleet payment validated"
            )
        )
        access_request.save(
            update_fields=[
                "status",
                "decided_by",
                "decided_at",
                "decision_note",
                "exempted",
                "resolution_event",
                "reason",
            ]
        )

    purge_timeout_access_requests(
        plate=access_request.plate,
        site_id=access_request.device.site_id,
    )
    pulse_device(access_request.device)
    access_request.refresh_from_db()
    return access_request


def deny_access_request(
    access_request: AccessRequest,
    user,
    *,
    note: str = "",
) -> AccessRequest:
    """Control-room deny: write Event(denied), leave barrier closed."""
    with transaction.atomic():
        access_request = (
            AccessRequest.objects.select_for_update()
            .select_related("device")
            .get(pk=access_request.pk)
        )
        if access_request.status != AccessRequest.Status.PENDING:
            raise ValueError(
                f"Access request {access_request.pk} is not pending "
                f"(status={access_request.status})"
            )

        from payments.services import close_open_payment_intents_for_access_request

        close_open_payment_intents_for_access_request(access_request)

        event = Event.objects.create(
            device=access_request.device,
            plate=access_request.plate,
            vehicle_type=access_request.vehicle_type,
            action=access_request.action,
            decision=Event.Decision.DENIED,
        )
        access_request.status = AccessRequest.Status.DENIED
        access_request.decided_by = user
        access_request.decided_at = timezone.now()
        access_request.decision_note = (note or "").strip()
        access_request.resolution_event = event
        access_request.save(
            update_fields=[
                "status",
                "decided_by",
                "decided_at",
                "decision_note",
                "resolution_event",
            ]
        )
    purge_timeout_access_requests(
        plate=access_request.plate,
        site_id=access_request.device.site_id,
    )
    return access_request


def update_session(
    session: Session,
    *,
    user=None,
    plate: str | None = None,
    billing_exempt: bool | None = None,
    note: str = "",
) -> Session:
    """
    Operator correction: change plate and/or exemption, or append a note.

    Paid sessions may only receive notes — plate/exemption edits are blocked.
    Entry/exit Event rows stay untouched so gate audit history is preserved.
    """
    from parking.models import SessionAuditEntry
    from rest_framework.exceptions import ValidationError

    note = (note or "").strip()
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=session.pk)
        paid_locked = session.payment_status == Session.PaymentStatus.PAID

        if paid_locked and (
            (plate is not None and plate != session.plate)
            or (
                billing_exempt is not None
                and billing_exempt != session.billing_exempt
            )
        ):
            raise ValidationError(
                {
                    "detail": "Paid sessions cannot change plate or exemption. "
                    "You can still add a note."
                }
            )

        update_fields: list[str] = []

        if (
            not paid_locked
            and plate is not None
            and plate != session.plate
        ):
            from wallets.services import get_wallet_for_plate, normalize_plate

            old_plate = session.plate
            cleaned = normalize_plate(plate)
            session.plate = cleaned
            update_fields.append("plate")
            # Plate correction: re-bind billing wallet to the corrected plate's owner.
            new_wallet = get_wallet_for_plate(cleaned)
            if session.wallet_id != (new_wallet.pk if new_wallet else None):
                session.wallet = new_wallet
                update_fields.append("wallet")
            SessionAuditEntry.objects.create(
                session=session,
                actor=user if getattr(user, "is_authenticated", False) else None,
                action=SessionAuditEntry.Action.PLATE_CHANGED,
                field="plate",
                old_value=old_plate,
                new_value=cleaned,
                note=note,
            )

        if (
            not paid_locked
            and billing_exempt is not None
            and billing_exempt != session.billing_exempt
        ):
            old = session.billing_exempt
            session.billing_exempt = billing_exempt
            session.allow_negative_balance = not billing_exempt
            update_fields.extend(["billing_exempt", "allow_negative_balance"])
            if billing_exempt and session.payment_status == Session.PaymentStatus.PENDING:
                session.payment_status = Session.PaymentStatus.EXEMPTED
                session.waiver_kind = Session.WaiverKind.OPERATOR
                update_fields.extend(["payment_status", "waiver_kind"])
                if session.fee is None and session.end_time is not None:
                    session.fee = session.estimated_fee(session.end_time)
                    update_fields.append("fee")
            elif billing_exempt:
                session.waiver_kind = Session.WaiverKind.OPERATOR
                update_fields.append("waiver_kind")
            elif (
                not billing_exempt
                and session.payment_status == Session.PaymentStatus.EXEMPTED
                and session.end_time is None
            ):
                session.payment_status = Session.PaymentStatus.PENDING
                session.waiver_kind = ""
                update_fields.extend(["payment_status", "waiver_kind"])
            elif not billing_exempt:
                session.waiver_kind = ""
                update_fields.append("waiver_kind")

            SessionAuditEntry.objects.create(
                session=session,
                actor=user if getattr(user, "is_authenticated", False) else None,
                action=(
                    SessionAuditEntry.Action.EXEMPTED
                    if billing_exempt
                    else SessionAuditEntry.Action.UNEXEMPTED
                ),
                field="billing_exempt",
                old_value=str(old),
                new_value=str(billing_exempt),
                note=note,
            )
            if billing_exempt and session.end_time is not None:
                resolve_pending_access_requests_for_session(
                    session,
                    user=user,
                    note=note or "Waived from session detail",
                    exempted=True,
                )

        # Note-only update (including paid sessions).
        if note and not update_fields:
            SessionAuditEntry.objects.create(
                session=session,
                actor=user if getattr(user, "is_authenticated", False) else None,
                action=SessionAuditEntry.Action.NOTE,
                field="note",
                old_value="",
                new_value=note[:500],
                note=note,
            )

        if update_fields:
            seen: set[str] = set()
            ordered: list[str] = []
            for field in update_fields:
                if field not in seen:
                    seen.add(field)
                    ordered.append(field)
            session.save(update_fields=ordered)

    return (
        Session.objects.select_related(
            "site",
            "site__project",
            "wallet",
            "entry_device",
            "start_event",
            "start_event__device",
            "start_event__device__site",
            "end_event",
            "end_event__device",
            "end_event__device__site",
        )
        .prefetch_related("audit_entries", "audit_entries__actor", "transactions")
        .get(pk=session.pk)
    )


def resolve_pending_access_requests_for_session(
    session: Session,
    *,
    user=None,
    note: str = "",
    exempted: bool = False,
) -> int:
    """Clear pending ARs for this plate/site after session-side settlement."""
    note = (note or "").strip() or "Resolved from session detail"
    now = timezone.now()
    pending = list(
        AccessRequest.objects.select_for_update().filter(
            plate=session.plate,
            device__site_id=session.site_id,
            status=AccessRequest.Status.PENDING,
        )
    )
    for access_request in pending:
        access_request.status = AccessRequest.Status.APPROVED
        access_request.decided_by = (
            user if getattr(user, "is_authenticated", False) else None
        )
        access_request.decided_at = now
        access_request.decision_note = note
        access_request.exempted = exempted
        access_request.save(
            update_fields=[
                "status",
                "decided_by",
                "decided_at",
                "decision_note",
                "exempted",
            ]
        )
    return len(pending)


def settle_session(
    session: Session,
    *,
    user=None,
    note: str = "",
) -> Session:
    """
    Validate/settle a closed unpaid wallet session via the operator wallet.

    Charges the acting user's operator wallet (negative OK), stamps paid-exit
    grace, clears pending ARs, and pulses the exit gate when available.
    """
    from decimal import Decimal

    from parking.models import SessionAuditEntry
    from rest_framework.exceptions import ValidationError

    note = (note or "").strip()
    device_to_pulse = None
    with transaction.atomic():
        session = (
            Session.objects.select_for_update(of=("self",))
            .select_related("wallet", "site", "zone")
            .get(pk=session.pk)
        )
        if session.end_time is None:
            raise ValidationError(
                {"detail": "Session is still open — end it before settling."}
            )
        if session.payment_status == Session.PaymentStatus.PAID:
            raise ValidationError({"detail": "Session is already paid."})
        if session.payment_status == Session.PaymentStatus.EXEMPTED:
            raise ValidationError(
                {"detail": "Session is waived — clear exemption before settling."}
            )
        if session.billing_method == Session.BillingMethod.CARD:
            raise ValidationError(
                {"detail": "Card sessions settle via kiosk / pay-at-exit."}
            )

        operator_wallet = get_operator_wallet(user)
        if operator_wallet is None:
            raise ValidationError(
                {
                    "detail": (
                        "Assign an operator wallet to your user before "
                        "settling / validating payment."
                    )
                }
            )

        fee = (
            session.fee
            if session.fee is not None
            else session.estimated_fee(session.end_time)
        )
        fee = Decimal(str(fee)).quantize(Decimal("0.01"))
        now = timezone.now()
        _charge_operator_wallet_for_session(
            operator_wallet=operator_wallet,
            session=session,
            amount=fee,
            user=user,
            reference=f"operator-settle-session-{session.pk}-{int(now.timestamp())}",
        )

        session.billing_exempt = False
        session.allow_negative_balance = True
        session.payment_status = Session.PaymentStatus.PAID
        session.billing_method = Session.BillingMethod.WALLET
        session.waiver_kind = ""
        session.fee = fee
        session.paid_exit_until = _paid_exit_grace_until(session, at=now)
        session.save(
            update_fields=[
                "billing_exempt",
                "allow_negative_balance",
                "payment_status",
                "billing_method",
                "waiver_kind",
                "fee",
                "paid_exit_until",
            ]
        )

        SessionAuditEntry.objects.create(
            session=session,
            actor=user if getattr(user, "is_authenticated", False) else None,
            action=SessionAuditEntry.Action.NOTE,
            field="payment_status",
            old_value="pending",
            new_value="operator_settle",
            note=note
            or (
                f"Settled via operator wallet #{operator_wallet.pk} "
                f"(allow negative balance)"
            ),
        )

        pending_exit = (
            AccessRequest.objects.select_for_update()
            .select_related("device")
            .filter(
                plate=session.plate,
                device__site_id=session.site_id,
                action=Event.Action.EXIT,
                status=AccessRequest.Status.PENDING,
            )
            .order_by("-created_at")
            .first()
        )
        if pending_exit is not None:
            device_to_pulse = pending_exit.device

        resolve_pending_access_requests_for_session(
            session,
            user=user,
            note=note or "Validated via session settle",
            exempted=False,
        )

    if device_to_pulse is None and session.end_event_id:
        end_event = (
            Event.objects.select_related("device")
            .filter(pk=session.end_event_id)
            .first()
        )
        if end_event is not None:
            device_to_pulse = end_event.device

    if device_to_pulse is not None:
        pulse_device(device_to_pulse)

    session.refresh_from_db()
    if session.payment_status != Session.PaymentStatus.PAID:
        raise ValidationError(
            {
                "detail": "Charge did not complete. Check operator wallet and try again."
            }
        )

    return (
        Session.objects.select_related(
            "site",
            "site__project",
            "wallet",
            "entry_device",
            "start_event",
            "start_event__device",
            "start_event__device__site",
            "end_event",
            "end_event__device",
            "end_event__device__site",
        )
        .prefetch_related("audit_entries", "audit_entries__actor", "transactions")
        .get(pk=session.pk)
    )


def end_session(
    session: Session,
    *,
    user=None,
    note: str = "",
    end_time=None,
) -> Session:
    """
    Manually close an open session by writing a granted EXIT event.

    Billing follows the normal session-end charge signal path.
    """
    from parking.models import Event, SessionAuditEntry
    from rest_framework.exceptions import ValidationError

    note = (note or "").strip()
    when = end_time or timezone.now()

    with transaction.atomic():
        session = (
            Session.objects.select_for_update(of=("self",))
            .select_related("site", "entry_device")
            .get(pk=session.pk)
        )
        if session.end_time is not None:
            raise ValidationError({"detail": "Session is already closed."})

        exit_device = (
            session.site.devices.filter(type="exit", enabled=True)
            .order_by("id")
            .first()
            or session.entry_device
        )
        event = Event(
            device=exit_device,
            plate=session.plate,
            action=Event.Action.EXIT,
            decision=Event.Decision.GRANTED,
        )
        event.save()
        if end_time is not None:
            Event.objects.filter(pk=event.pk).update(created_at=when)
            event.refresh_from_db()

        # Ensure close even if the signal did not match (e.g. plate edits).
        session.refresh_from_db()
        if session.end_time is None:
            session.end_event = event
            session.end_time = when
            session.save(update_fields=["end_event", "end_time"])
        elif end_time is not None and session.end_time != when:
            session.end_time = when
            session.save(update_fields=["end_time"])

        SessionAuditEntry.objects.create(
            session=session,
            actor=user if getattr(user, "is_authenticated", False) else None,
            action=SessionAuditEntry.Action.ENDED,
            field="end_time",
            old_value="",
            new_value=when.isoformat(),
            note=note or "Ended manually from session form",
        )

    return (
        Session.objects.select_related(
            "site",
            "site__project",
            "entry_device",
            "start_event",
            "start_event__device",
            "start_event__device__site",
            "end_event",
            "end_event__device",
            "end_event__device__site",
        )
        .prefetch_related("audit_entries", "audit_entries__actor", "transactions")
        .get(pk=session.pk)
    )
