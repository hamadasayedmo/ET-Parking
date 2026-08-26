"""Nested zone session stack helpers (entry/exit, grace, stack fees)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from parking.models import Session
from wallets.services import calculate_parking_fee, normalize_plate


def session_slice_fee(session: Session, *, at_time) -> Decimal:
    """Fee for one session slice (visit grace → 0)."""
    if session.within_visit_grace(at_time):
        return Decimal("0.00")
    rate_source = session.zone or session.site
    return calculate_parking_fee(session.start_time, at_time, rate_source=rate_source)


def stack_total_fee(sessions: list[Session], *, at_time) -> Decimal:
    return sum((session_slice_fee(s, at_time=at_time) for s in sessions), Decimal("0.00"))


def find_open_for_zone(*, plate: str, site_id: int, zone_id: int) -> Session | None:
    plate = normalize_plate(plate)
    return (
        Session.objects.select_related("zone", "site", "entry_device", "parent_session")
        .filter(
            plate=plate,
            site_id=site_id,
            zone_id=zone_id,
            end_time__isnull=True,
        )
        .order_by("-start_time")
        .first()
    )


def open_sessions_for_plate(*, plate: str, site_id: int) -> list[Session]:
    plate = normalize_plate(plate)
    return list(
        Session.objects.select_for_update(of=("self",))
        .select_related(
            "zone", "site", "site__project", "entry_device", "parent_session"
        )
        .filter(plate=plate, site_id=site_id, end_time__isnull=True)
        .order_by("-start_time")
    )


def has_open_child(session: Session, open_sessions: list[Session]) -> bool:
    return any(s.parent_session_id == session.pk for s in open_sessions)


def apply_nested_grace(outers: list[Session], *, settled_at) -> None:
    for outer in outers:
        grace = 0
        if outer.site_id:
            grace = outer.site.effective_grace_minutes()
        outer.nested_settled_at = settled_at
        outer.grace_exit_until = (
            settled_at + timedelta(minutes=grace) if grace else settled_at
        )
        outer.save(update_fields=["nested_settled_at", "grace_exit_until"])


def close_session(session: Session, *, event, end_time, fee: Decimal | None = None) -> None:
    update_fields = ["end_event", "end_time"]
    session.end_event = event
    session.end_time = end_time
    if fee is not None:
        session.fee = fee
        update_fields.append("fee")
    if session.within_visit_grace(end_time) or session.within_nested_grace(end_time):
        session.billing_exempt = True
        session.payment_status = Session.PaymentStatus.EXEMPTED
        session.waiver_kind = Session.WaiverKind.GRACE
        session.fee = Decimal("0.00")
        update_fields.extend(
            ["billing_exempt", "payment_status", "waiver_kind", "fee"]
        )
    session.save(update_fields=list(dict.fromkeys(update_fields)))


def collect_outers(inner: Session, open_sessions: list[Session]) -> list[Session]:
    by_id = {s.pk: s for s in open_sessions}
    outers: list[Session] = []
    node = inner
    seen = set()
    while node.parent_session_id and node.parent_session_id not in seen:
        seen.add(node.parent_session_id)
        parent = by_id.get(node.parent_session_id)
        if parent is None:
            break
        outers.append(parent)
        node = parent
    return outers
