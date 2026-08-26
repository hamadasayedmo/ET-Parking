from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from parking.models import Session

from .models import Transaction, Wallet
from .services import get_wallet_for_plate, plate_wallet_is_exempted


@receiver(post_save, sender=Session)
def charge_wallet_on_session_end(sender, instance, created, **kwargs):
    """Bill the wallet when a parking session is closed (exit or re-entry cleanup)."""
    if created or instance.end_time is None:
        return
    if instance.payment_status != Session.PaymentStatus.PENDING:
        return

    update_fields = kwargs.get("update_fields")
    if update_fields is not None:
        # Re-run only when the session is closed or billing flags change — not on
        # fee-only writes after a declined withdraw (that would recurse).
        relevant = {
            "end_time",
            "allow_negative_balance",
            "billing_exempt",
            "payment_status",
            "wallet",
        }
        if not relevant.intersection(update_fields):
            return

    with transaction.atomic():
        session = (
            Session.objects.select_for_update(of=("self",))
            .select_related(
                "zone",
                "site",
                "site__project",
                "wallet",
                "entry_device",
                "end_event",
                "end_event__device",
            )
            .filter(pk=instance.pk, payment_status=Session.PaymentStatus.PENDING)
            .first()
        )
        if session is None or session.end_time is None:
            return

        if session.billing_method == Session.BillingMethod.CARD:
            # Card/kiosk path — unless control room waived billing_exempt.
            if not session.billing_exempt:
                return
            fee = (
                session.fee
                if session.fee is not None
                else session.estimated_fee(session.end_time)
            )
            session.fee = fee
            session.payment_status = Session.PaymentStatus.EXEMPTED
            if not session.waiver_kind:
                session.waiver_kind = Session.WaiverKind.OPERATOR
            session.save(
                update_fields=[
                    "fee",
                    "payment_status",
                    "waiver_kind",
                ]
            )
            return

        fee = (
            session.fee
            if session.fee is not None
            else session.estimated_fee(session.end_time)
        )

        wallet = session.wallet
        if wallet is None:
            wallet = get_wallet_for_plate(session.plate)
            if wallet is not None and session.wallet_id is None:
                session.wallet = wallet
                session.save(update_fields=["wallet"])

        if session.billing_exempt or (
            wallet is not None and wallet.is_active and wallet.is_exempted
        ) or (wallet is None and plate_wallet_is_exempted(session.plate)):
            session.fee = fee
            session.billing_exempt = True
            session.payment_status = Session.PaymentStatus.EXEMPTED
            if not session.waiver_kind:
                session.waiver_kind = Session.WaiverKind.POLICY
            session.save(
                update_fields=[
                    "fee",
                    "billing_exempt",
                    "payment_status",
                    "waiver_kind",
                ]
            )
            return

        if wallet is None or not wallet.is_active:
            # No billable wallet — not a failed payment attempt.
            session.fee = fee
            session.billing_exempt = True
            session.payment_status = Session.PaymentStatus.EXEMPTED
            session.waiver_kind = Session.WaiverKind.GUEST
            session.save(
                update_fields=[
                    "fee",
                    "billing_exempt",
                    "payment_status",
                    "waiver_kind",
                ]
            )
            return

        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)

        ok = wallet.withdraw(
            fee,
            session=session,
            reference=f"session-{session.pk}-{int(session.end_time.timestamp())}",
            transaction_type=Transaction.Type.PARKING_FEE,
            allow_negative=session.allow_negative_balance,
        )

        if ok:
            session.fee = fee
            session.payment_status = Session.PaymentStatus.PAID
            session.save(update_fields=["fee", "payment_status"])
            return

        # Never mark sessions failed — leave pending and raise control-room AR.
        Session.objects.filter(pk=session.pk).update(fee=fee)
        from parking.services import escalate_unsettled_wallet_session

        escalate_unsettled_wallet_session(session, wallet=wallet, fee=fee)
