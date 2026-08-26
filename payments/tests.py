from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from parking.models import AccessRequest, Session, SessionAuditEntry
from parking.services import authorize_plate
from payments.models import PaymentIntent
from payments.services import (
    apply_payment_result,
    close_open_payment_intents_for_access_request,
    create_control_room_payment_intent,
    create_payment_intent_for_access_request,
    refresh_payment_intent_amount,
)
from projects.models import Project
from sites.models import Device, Site
from wallets.models import Vehicle, Wallet


class BillingModeAuthorizeTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Hybrid Lot")
        self.site = Site.objects.create(
            project=self.project,
            name="Lot A",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                ip="192.168.0.10",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
            self.exit = Device.objects.create(
                site=self.site,
                ip="192.168.0.11",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )
        from payments.models import KioskCredential

        KioskCredential.objects.create(
            device=self.exit,
            nearpay_terminal_id="tid-hybrid-exit",
            is_active=True,
        )
        self.wallet = Wallet.objects.create(
            project=self.project, name="Acme", is_active=True
        )
        Vehicle.objects.create(wallet=self.wallet, plate="ABC123", is_active=True)
        self.wallet.balance = Decimal("20.00")
        self.wallet.save(update_fields=["balance"])

    def test_both_off_never_auto_grants(self):
        self.project.wallet_enabled = False
        self.project.pay_at_exit_enabled = False
        self.project.save()

        result = authorize_plate(self.entry, "ABC123")
        self.assertFalse(result.granted)
        self.assertIsNotNone(result.access_request)
        self.assertIn("Manual mode", result.access_request.reason)

    def test_pay_at_exit_only_unknown_entry_grants_card_session(self):
        self.project.wallet_enabled = False
        self.project.pay_at_exit_enabled = True
        self.project.save()

        with patch("parking.services.publish_once", return_value=True):
            result = authorize_plate(self.entry, "UNK999")
        self.assertTrue(result.granted)
        session = Session.objects.get(plate="UNK999", end_time__isnull=True)
        self.assertEqual(session.billing_method, Session.BillingMethod.CARD)

    def test_pay_at_exit_exit_holds_for_payment(self):
        self.project.wallet_enabled = False
        self.project.pay_at_exit_enabled = True
        self.project.save()

        with patch("parking.services.publish_once", return_value=True):
            authorize_plate(self.entry, "UNK888")
            result = authorize_plate(self.exit, "UNK888")
        self.assertFalse(result.granted)
        self.assertEqual(result.access_request.reason, "Awaiting exit payment")
        self.assertTrue(
            PaymentIntent.objects.filter(
                access_request=result.access_request,
                status=PaymentIntent.Status.PENDING,
            ).exists()
        )

    def test_hybrid_funded_uses_wallet(self):
        self.project.wallet_enabled = True
        self.project.pay_at_exit_enabled = True
        self.project.save()

        with patch("parking.services.publish_once", return_value=True):
            result = authorize_plate(self.entry, "ABC123")
        self.assertTrue(result.granted)
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        self.assertEqual(session.billing_method, Session.BillingMethod.WALLET)

    def test_hybrid_unknown_uses_card(self):
        self.project.wallet_enabled = True
        self.project.pay_at_exit_enabled = True
        self.project.save()

        with patch("parking.services.publish_once", return_value=True):
            result = authorize_plate(self.entry, "GUEST1")
        self.assertTrue(result.granted)
        session = Session.objects.get(plate="GUEST1", end_time__isnull=True)
        self.assertEqual(session.billing_method, Session.BillingMethod.CARD)


class PayAtExitKioskFlowTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Kiosk Pay Lot",
            wallet_enabled=False,
            pay_at_exit_enabled=True,
            currency="SAR",
        )
        self.site = Site.objects.create(
            project=self.project,
            name="Lot A",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                ip="10.0.0.1",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
            self.exit = Device.objects.create(
                site=self.site,
                ip="10.0.0.2",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )
        from payments.models import KioskCredential

        KioskCredential.objects.create(
            device=self.exit,
            nearpay_terminal_id="tid-test-exit",
            label="Test exit kiosk",
            is_active=True,
        )
        self.operator = get_user_model().objects.create_user(
            username="payment-control",
            password="pass",
            is_staff=True,
        )

    def _open_card_session(self, plate="KSK001"):
        with patch("parking.services.publish_once", return_value=True):
            authorize_plate(self.entry, plate)
            return authorize_plate(self.exit, plate)

    def test_exit_creates_pending_intent(self):
        result = self._open_card_session()
        self.assertFalse(result.granted)
        intent = PaymentIntent.objects.get(access_request=result.access_request)
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
        self.assertEqual(intent.provider, "")
        self.assertEqual(intent.currency, "SAR")

    def test_exit_without_kiosk_creates_ar_only(self):
        from payments.models import KioskCredential

        KioskCredential.objects.filter(device=self.exit).delete()
        result = self._open_card_session("NOKSK1")
        self.assertFalse(result.granted)
        self.assertIsNotNone(result.access_request)
        self.assertEqual(
            result.access_request.reason,
            "No kiosk paired — decide manually",
        )
        self.assertFalse(
            PaymentIntent.objects.filter(
                access_request=result.access_request
            ).exists()
        )

    def test_ocr_flip_keeps_pending_ar(self):
        first = self._open_card_session("ABC123")
        ar_id = first.access_request.pk
        intent_id = PaymentIntent.objects.get(access_request_id=ar_id).pk

        with patch("parking.services.publish_once", return_value=True):
            second = authorize_plate(self.exit, "ABC12B")  # dist 1

        self.assertEqual(second.access_request.pk, ar_id)
        self.assertTrue(
            PaymentIntent.objects.filter(pk=intent_id, status=PaymentIntent.Status.PENDING).exists()
        )
        second.access_request.refresh_from_db()
        self.assertEqual(second.access_request.plate, "ABC123")
        self.assertIn("OCR also read ABC12B", second.access_request.reason)

    def test_approve_without_charge_requires_note(self):
        from parking.services import approve_access_request

        result = self._open_card_session("NOTE01")
        with self.assertRaises(ValueError) as ctx:
            approve_access_request(
                result.access_request,
                self.operator,
                exempted=True,
                note="",
            )
        self.assertIn("note", str(ctx.exception).lower())

    def test_approve_without_charge_stamps_operator_waiver(self):
        from parking.models import Session
        from parking.services import approve_access_request

        result = self._open_card_session("STAMP1")
        with patch("parking.services.publish_once", return_value=True):
            approve_access_request(
                result.access_request,
                self.operator,
                note="Cash at desk",
            )
        result.access_request.refresh_from_db()
        self.assertTrue(result.access_request.exempted)
        self.assertEqual(result.access_request.decision_note, "Cash at desk")
        session = Session.objects.get(plate="STAMP1")
        self.assertEqual(session.waiver_kind, Session.WaiverKind.OPERATOR)
        self.assertEqual(session.payment_status, Session.PaymentStatus.EXEMPTED)
    @patch("parking.services.publish_once", return_value=True)
    def test_complete_paid_opens_gate(self, mock_pub):
        result = self._open_card_session()
        intent = PaymentIntent.objects.get(access_request=result.access_request)

        apply_payment_result(
            intent,
            status="paid",
            provider="nearpay",
            provider_transaction_id="np-tx-1",
            raw_result={"ok": True},
        )
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PAID)
        self.assertEqual(intent.provider, "nearpay")
        result.access_request.refresh_from_db()
        self.assertEqual(result.access_request.status, AccessRequest.Status.APPROVED)
        session = Session.objects.get(plate="KSK001")
        self.assertEqual(session.payment_status, Session.PaymentStatus.PAID)
        mock_pub.assert_called()

    def test_complete_idempotent(self):
        result = self._open_card_session()
        intent = PaymentIntent.objects.get(access_request=result.access_request)
        with patch("parking.services.publish_once", return_value=True):
            apply_payment_result(
                intent, status="paid", provider_transaction_id="np-1"
            )
            again = apply_payment_result(
                intent, status="paid", provider_transaction_id="np-1"
            )
        self.assertEqual(again.status, PaymentIntent.Status.PAID)

    def test_complete_failed_keeps_same_bill_open_for_retry(self):
        result = self._open_card_session()
        intent = PaymentIntent.objects.get(access_request=result.access_request)

        apply_payment_result(
            intent,
            status="failed",
            provider="nearpay",
            raw_result={"message": "declined"},
        )

        intent.refresh_from_db()
        result.access_request.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
        self.assertEqual(result.access_request.status, AccessRequest.Status.PENDING)
        self.assertEqual(
            result.access_request.reason,
            "Exit payment issue — driver can retry",
        )

    def test_cancelled_bill_can_be_resent_for_same_open_session(self):
        result = self._open_card_session("RESEND1")
        intent = PaymentIntent.objects.get(access_request=result.access_request)

        close_open_payment_intents_for_access_request(
            result.access_request,
            status=PaymentIntent.Status.FAILED,
        )
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.FAILED)

        resent = create_payment_intent_for_access_request(result.access_request)

        self.assertNotEqual(resent.pk, intent.pk)
        self.assertEqual(resent.status, PaymentIntent.Status.PENDING)
        self.assertEqual(resent.session_id, intent.session_id)
        result.access_request.refresh_from_db()
        self.assertEqual(result.access_request.status, AccessRequest.Status.PENDING)

    def test_resend_reuses_existing_open_bill(self):
        result = self._open_card_session("RESEND2")
        intent = PaymentIntent.objects.get(access_request=result.access_request)

        again = create_payment_intent_for_access_request(result.access_request)

        self.assertEqual(again.pk, intent.pk)
        self.assertEqual(
            PaymentIntent.objects.filter(
                access_request=result.access_request
            ).count(),
            1,
        )

    def test_refresh_updates_open_bill_amount(self):
        result = self._open_card_session("REFRESH1")
        intent = PaymentIntent.objects.get(access_request=result.access_request)
        intent.amount = Decimal("99.00")
        intent.save(update_fields=["amount", "updated_at"])

        refreshed = refresh_payment_intent_amount(result.access_request)

        self.assertEqual(refreshed.pk, intent.pk)
        self.assertNotEqual(refreshed.amount, Decimal("99.00"))
        self.assertEqual(
            refreshed.amount,
            intent.session.estimated_fee(),
        )

    def test_charge_at_kiosk_blocked_when_unpaid_open_session_exists(self):
        result = self._open_card_session("BLOCK1")
        close_open_payment_intents_for_access_request(
            result.access_request,
            status=PaymentIntent.Status.FAILED,
        )
        with self.assertRaises(ValueError) as ctx:
            # Service still allows new_session path to raise unpaid; view blocks earlier.
            # Call serializer path equivalent: create_control_room with new_session.
            create_control_room_payment_intent(
                result.access_request,
                mode="new_session",
                entry_time=timezone.now() - timedelta(hours=1),
                actor=self.operator,
            )
        self.assertIn("unpaid session", str(ctx.exception).lower())

    @patch("parking.services.publish_once", return_value=True)
    def test_late_paid_after_control_cancel_settles_and_opens_pending_ar(
        self,
        mock_pub,
    ):
        result = self._open_card_session()
        intent = PaymentIntent.objects.get(access_request=result.access_request)

        close_open_payment_intents_for_access_request(
            result.access_request,
            status=PaymentIntent.Status.FAILED,
        )
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.FAILED)

        apply_payment_result(intent, status="paid", provider="nearpay")

        intent.refresh_from_db()
        result.access_request.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PAID)
        self.assertEqual(result.access_request.status, AccessRequest.Status.APPROVED)
        mock_pub.assert_called()

    def test_late_paid_after_queue_swap_settles_without_pulse(self):
        result = self._open_card_session("LATE001")
        ar_id = result.access_request.pk
        intent = PaymentIntent.objects.get(access_request=result.access_request)
        with patch("parking.services.publish_once", return_value=True):
            authorize_plate(self.exit, "NEXT002")
        intent.refresh_from_db()
        self.assertFalse(
            AccessRequest.objects.filter(pk=ar_id).exists(),
            "Superseded pending AR should be deleted on queue swap",
        )
        self.assertEqual(intent.status, PaymentIntent.Status.CANCELLED)

        with patch("parking.services.publish_once") as mock_pub:
            apply_payment_result(intent, status="paid", provider="nearpay")
        intent.refresh_from_db()
        session = Session.objects.get(plate="LATE001")
        self.assertEqual(intent.status, PaymentIntent.Status.PAID)
        self.assertEqual(session.payment_status, Session.PaymentStatus.PAID)
        self.assertIsNone(session.end_time)
        mock_pub.assert_not_called()

    def test_paid_session_reexits_automatically_within_grace(self):
        result = self._open_card_session("GRACE01")
        intent = PaymentIntent.objects.get(access_request=result.access_request)
        self.site.grace_minutes = 10
        self.site.save(update_fields=["grace_minutes"])
        session = Session.objects.get(pk=intent.session_id)
        session.start_time = timezone.now() - timedelta(hours=1)
        session.save(update_fields=["start_time"])
        with patch("parking.services.publish_once", return_value=True):
            apply_payment_result(intent, status="paid")
            second_exit = authorize_plate(self.exit, "GRACE01")
        self.assertTrue(second_exit.granted)
        self.assertIsNone(second_exit.access_request)

    def test_extend_previous_bills_only_after_paid_grace(self):
        first = self._open_card_session("EXT001")
        initial = PaymentIntent.objects.get(access_request=first.access_request)
        with patch("parking.services.publish_once", return_value=True):
            apply_payment_result(initial, status="paid")
        session = Session.objects.get(plate="EXT001")
        session.paid_exit_until = timezone.now() - timedelta(hours=2)
        session.save(update_fields=["paid_exit_until"])

        second = authorize_plate(self.exit, "EXT001")
        self.assertFalse(second.granted)
        self.assertFalse(
            PaymentIntent.objects.filter(
                access_request=second.access_request,
                status=PaymentIntent.Status.PENDING,
            ).exists()
        )
        intent = create_control_room_payment_intent(
            second.access_request,
            mode="extend_previous",
            actor=self.operator,
        )
        self.assertEqual(intent.session_id, session.pk)
        self.assertEqual(intent.amount, Decimal("5.00"))
        self.assertTrue(
            SessionAuditEntry.objects.filter(
                session=session,
                field="paid_exit_until",
            ).exists()
        )

    def test_new_session_audits_administrative_split(self):
        with patch("parking.services.publish_once", return_value=True):
            authorize_plate(self.entry, "SPLIT01")
        old = Session.objects.get(plate="SPLIT01", end_time__isnull=True)
        old.start_time = timezone.now() - timedelta(hours=3)
        old.payment_status = Session.PaymentStatus.PAID
        old.billing_method = Session.BillingMethod.CARD
        old.paid_exit_until = timezone.now() - timedelta(hours=1)
        old.save(
            update_fields=[
                "start_time",
                "payment_status",
                "billing_method",
                "paid_exit_until",
            ]
        )
        access = authorize_plate(self.exit, "SPLIT01").access_request
        entry_time = timezone.now() - timedelta(minutes=30)
        intent = create_control_room_payment_intent(
            access,
            mode="new_session",
            entry_time=entry_time,
            actor=self.operator,
        )

        old.refresh_from_db()
        self.assertEqual(old.end_time, entry_time)
        self.assertNotEqual(intent.session_id, old.pk)
        self.assertEqual(intent.session.start_time, entry_time)
        self.assertEqual(intent.session.billing_method, Session.BillingMethod.CARD)
        audit = SessionAuditEntry.objects.get(
            session=old,
            action=SessionAuditEntry.Action.ENDED,
        )
        self.assertIn(str(access.pk), audit.note)
        self.assertIn(str(intent.session_id), audit.note)
        new_audit = SessionAuditEntry.objects.get(
            session_id=intent.session_id,
            field="start_time",
        )
        self.assertIn(str(old.pk), new_audit.note)


class ZatcaPhase1Tests(TestCase):
    def test_split_vat_inclusive(self):
        from payments.zatca import split_vat_inclusive

        net, vat, total = split_vat_inclusive(Decimal("3.00"))
        self.assertEqual(total, Decimal("3.00"))
        self.assertEqual(vat, Decimal("0.39"))
        self.assertEqual(net, Decimal("2.61"))

    def test_phase1_qr_known_vector(self):
        """ZATCA sample seller / VAT / totals encode to a fixed Base64 string."""
        import base64

        from payments.zatca import build_phase1_qr_base64, tlv_encode

        qr = build_phase1_qr_base64(
            seller_name="Bobs Basement Records",
            vat_number="310175397400003",
            invoice_timestamp="2022-04-25T15:30:00Z",
            invoice_total_with_vat="100.00",
            vat_amount="15.00",
        )
        raw = base64.b64decode(qr)
        expected = (
            tlv_encode(1, "Bobs Basement Records")
            + tlv_encode(2, "310175397400003")
            + tlv_encode(3, "2022-04-25T15:30:00Z")
            + tlv_encode(4, "100.00")
            + tlv_encode(5, "15.00")
        )
        self.assertEqual(raw, expected)
        self.assertEqual(qr, base64.b64encode(expected).decode("ascii"))


class ReceiptPayloadTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Receipt Lot",
            wallet_enabled=False,
            pay_at_exit_enabled=True,
            currency="SAR",
            country="SA",
            seller_name="Example Parking Co",
            vat_number="310175397400003",
        )
        self.site = Site.objects.create(
            project=self.project,
            name="Lot A",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                ip="10.0.1.1",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
            self.exit = Device.objects.create(
                site=self.site,
                ip="10.0.1.2",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )
        from payments.models import KioskCredential

        KioskCredential.objects.create(
            device=self.exit,
            nearpay_terminal_id="tid-receipt-exit",
            is_active=True,
        )

    def test_receipt_payload_flat_zatca_fields(self):
        from payments.receipts import receipt_payload

        with patch("parking.services.publish_once", return_value=True):
            authorize_plate(self.entry, "RCP001")
            result = authorize_plate(self.exit, "RCP001")
        intent = PaymentIntent.objects.get(access_request=result.access_request)
        with patch("parking.services.publish_once", return_value=True):
            apply_payment_result(
                intent,
                status="paid",
                provider="nearpay",
                provider_transaction_id="np-r1",
                card_last4="4242",
                raw_result={},
            )
        intent.refresh_from_db()
        payload = receipt_payload(intent)

        self.assertNotIn("zatca", payload)
        self.assertNotIn("paid", payload)
        self.assertNotIn("status", payload)
        self.assertNotIn("merchant_reference_id", payload)
        self.assertEqual(payload["country"], "SA")
        self.assertEqual(payload["seller_name"], "Example Parking Co")
        self.assertEqual(payload["vat_number"], "310175397400003")
        self.assertEqual(payload["vat_rate"], "15.00")
        self.assertEqual(payload["invoice_number"], intent.merchant_reference_id)
        self.assertTrue(payload["qr_base64"])
        self.assertEqual(payload["invoice_total_with_vat"], str(intent.amount))

    def test_receipt_payload_omits_qr_without_seller(self):
        from payments.receipts import receipt_payload

        self.project.seller_name = ""
        self.project.save(update_fields=["seller_name"])
        with patch("parking.services.publish_once", return_value=True):
            authorize_plate(self.entry, "RCP002")
            result = authorize_plate(self.exit, "RCP002")
        intent = PaymentIntent.objects.get(access_request=result.access_request)
        payload = receipt_payload(intent)
        self.assertEqual(payload["qr_base64"], "")
        self.assertEqual(payload["vat_rate"], "15.00")
        self.assertEqual(payload["seller_name"], "")

    def test_project_defaults_country_sa(self):
        p = Project.objects.create(name="Defaults")
        self.assertEqual(p.country, "SA")
        self.assertEqual(p.seller_name, "")
        self.assertEqual(p.vat_number, "")


class StaffTerminalPaymentsApiTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Terminal API Lot",
            wallet_enabled=False,
            pay_at_exit_enabled=True,
            currency="SAR",
        )
        self.site = Site.objects.create(
            project=self.project,
            name="Lot T",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                name="Entry T",
                ip="10.1.0.1",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
            self.exit = Device.objects.create(
                site=self.site,
                name="Exit T",
                ip="10.1.0.2",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )
        from payments.models import KioskCredential

        self.credential = KioskCredential.objects.create(
            device=self.exit,
            nearpay_terminal_id="tid-ops-1",
            label="Gate 1 kiosk",
            is_active=True,
        )
        self.staff = get_user_model().objects.create_user(
            username="term-staff",
            password="pass",
            is_staff=True,
        )
        self.client = __import__("rest_framework.test", fromlist=["APIClient"]).APIClient()
        self.client.force_authenticate(user=self.staff)

    def _open_and_pay(self, plate="TRM001"):
        with patch("parking.services.publish_once", return_value=True):
            authorize_plate(self.entry, plate)
            result = authorize_plate(self.exit, plate)
        intent = PaymentIntent.objects.get(access_request=result.access_request)
        apply_payment_result(
            intent,
            status="paid",
            provider="nearpay",
            provider_transaction_id="txn-trm-1",
            card_last4="4242",
        )
        intent.refresh_from_db()
        return intent

    def test_list_intents_includes_terminal_fields(self):
        intent = self._open_and_pay()
        response = self.client.get("/api/payments/intents/")
        self.assertEqual(response.status_code, 200)
        rows = response.data["results"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], intent.pk)
        self.assertEqual(row["plate"], "TRM001")
        self.assertEqual(row["device_id"], self.exit.pk)
        self.assertEqual(row["nearpay_terminal_id"], "tid-ops-1")
        self.assertEqual(row["kiosk_label"], "Gate 1 kiosk")
        self.assertEqual(row["kiosk_credential_id"], self.credential.pk)

    def test_list_intents_filter_by_credential(self):
        self._open_and_pay("TRM002")
        response = self.client.get(
            "/api/payments/intents/",
            {"credential": self.credential.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        response_empty = self.client.get(
            "/api/payments/intents/",
            {"credential": 999999},
        )
        self.assertEqual(response_empty.data["count"], 0)

    def test_export_csv_headers(self):
        self._open_and_pay("TRM003")
        response = self.client.get("/api/payments/intents/export/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode("utf-8")
        self.assertIn("nearpay_terminal_id", body)
        self.assertIn("TRM003", body)
        self.assertIn("tid-ops-1", body)

    def test_list_credentials_omits_token(self):
        response = self.client.get("/api/payments/kiosk-credentials/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["nearpay_terminal_id"], "tid-ops-1")
        self.assertEqual(row["label"], "Gate 1 kiosk")
        self.assertNotIn("token", row)

    def test_credentials_not_writable(self):
        response = self.client.post(
            "/api/payments/kiosk-credentials/",
            {"device": self.exit.pk, "nearpay_terminal_id": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 405)

