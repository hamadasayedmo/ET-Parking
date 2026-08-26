from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from parking.billing_context import pop_pending_billing_method
from parking.zone_sessions import (
    apply_nested_grace,
    close_session,
    collect_outers,
    has_open_child,
    open_sessions_for_plate,
    session_slice_fee,
)
from wallets.services import get_wallet_for_plate, plate_wallet_is_exempted

from .models import Event, Session


@receiver(post_save, sender=Event)
def handle_event_session(sender, instance, created, **kwargs):
    if not created or instance.decision != Event.Decision.GRANTED:
        return

    device = instance.device
    site = device.site
    zone = getattr(device, "zone", None)

    with transaction.atomic():
        open_sessions = open_sessions_for_plate(
            plate=instance.plate, site_id=site.pk
        )

        if instance.action == Event.Action.ENTRY:
            # Same-gate double entry: reopen only — keep existing session.
            for existing in open_sessions:
                if existing.entry_device_id == device.pk:
                    return

            parent_session = None
            if zone and zone.parent_id:
                for existing in open_sessions:
                    if existing.zone_id == zone.parent_id:
                        parent_session = existing
                        break

            if parent_session is None:
                # Non-nested re-entry: close prior open sessions at this site.
                for session in open_sessions:
                    session.end_time = instance.created_at
                    session.save(update_fields=["end_time"])

            wallet = get_wallet_for_plate(instance.plate)
            wallet_exempt = bool(
                wallet and wallet.is_active and wallet.is_exempted
            ) or plate_wallet_is_exempted(instance.plate)
            Session.objects.create(
                plate=instance.plate,
                wallet=wallet,
                site=site,
                zone=zone,
                parent_session=parent_session,
                entry_device=device,
                start_event=instance,
                start_time=instance.created_at,
                billing_method=pop_pending_billing_method(Session.BillingMethod.WALLET),
                billing_exempt=wallet_exempt,
                allow_negative_balance=not wallet_exempt,
            )

        elif instance.action == Event.Action.EXIT:
            if not open_sessions:
                return

            target = None
            if zone is not None:
                for session in open_sessions:
                    if session.zone_id == zone.pk:
                        target = session
                        break
            if target is None:
                target = open_sessions[0]

            if has_open_child(target, open_sessions):
                return

            wallet_exempt = plate_wallet_is_exempted(instance.plate)
            at = instance.created_at

            outers = collect_outers(target, open_sessions) if target.parent_session_id else []

            if outers:
                from decimal import Decimal

                stack = [target, *outers]
                total = sum(
                    (session_slice_fee(s, at_time=at) for s in stack),
                    Decimal("0.00"),
                )
                if wallet_exempt:
                    target.billing_exempt = True
                    total = Decimal("0.00")
                close_session(target, event=instance, end_time=at, fee=total)
                if wallet_exempt or total == 0:
                    target.payment_status = Session.PaymentStatus.EXEMPTED
                    target.billing_exempt = True
                    target.fee = Decimal("0.00")
                    if wallet_exempt and not target.waiver_kind:
                        target.waiver_kind = Session.WaiverKind.POLICY
                    elif total == 0 and not target.waiver_kind:
                        target.waiver_kind = Session.WaiverKind.GRACE
                    target.save(
                        update_fields=[
                            "payment_status",
                            "billing_exempt",
                            "fee",
                            "waiver_kind",
                        ]
                    )
                apply_nested_grace(outers, settled_at=at)
                return

            if wallet_exempt:
                target.billing_exempt = True
                target.allow_negative_balance = False
            close_session(target, event=instance, end_time=at)
