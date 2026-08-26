from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from parking.models import AccessRequest, Event, Session
from parking.services import authorize_plate, verify_plate_access
from sites.models import Device, Site
from projects.models import Project
from wallets.models import Transaction, Vehicle, Wallet
from wallets.services import calculate_parking_fee, minimum_entry_balance


class WalletBillingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Test Project")
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
        self.wallet = Wallet.objects.create(
            project=self.project, name="Acme", is_active=True
        )
        self.vehicle = Vehicle.objects.create(
            wallet=self.wallet,
            plate="ABC123",
            is_active=True,
        )
        self.wallet.balance = Decimal("20.00")
        self.wallet.save(update_fields=["balance"])

    def test_normalize_plate_removes_internal_spaces(self):
        from wallets.services import normalize_plate

        self.assertEqual(normalize_plate("98 54 VDR"), "9854VDR")
        self.assertEqual(normalize_plate("  abc 123 "), "ABC123")
        self.assertEqual(normalize_plate("xyz789"), "XYZ789")

    def test_authorize_matches_plate_with_spaces(self):
        result = authorize_plate(self.entry, "AB C 123")
        self.assertTrue(result.granted)
        self.assertEqual(result.event.plate, "ABC123")
        self.assertEqual(result.event.decision, Event.Decision.GRANTED)

    def test_wallet_defaults_zero_balance(self):
        wallet = Wallet.objects.create(
            project=self.project, name="New Co", is_active=True
        )
        self.assertEqual(wallet.balance, Decimal("0.00"))

    def test_deposit_and_withdraw(self):
        self.wallet.deposit(Decimal("5.00"), reference="dep-1")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("25.00"))
        self.assertTrue(self.wallet.withdraw(Decimal("3.00"), reference="wd-1"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("22.00"))
        self.assertFalse(self.wallet.withdraw(Decimal("100.00"), reference="wd-fail"))

    def test_entry_requires_minimum_balance(self):
        self.assertTrue(verify_plate_access("ABC123", device=self.entry))
        self.wallet.balance = minimum_entry_balance(self.site) - Decimal("0.01")
        self.wallet.save(update_fields=["balance"])
        self.assertFalse(verify_plate_access("ABC123", device=self.entry))
        self.assertEqual(
            minimum_entry_balance(self.site),
            self.site.hourly_rate + self.site.additional_fee,
        )

    def test_authorize_uses_device_type(self):
        result = authorize_plate(self.entry, "abc123")
        self.assertTrue(result.granted)
        self.assertEqual(result.event.action, Event.Action.ENTRY)
        self.assertEqual(result.event.decision, Event.Decision.GRANTED)
        self.assertEqual(result.event.plate, "ABC123")

        exit_result = authorize_plate(self.exit, "ABC123")
        self.assertTrue(exit_result.granted)
        self.assertEqual(exit_result.event.action, Event.Action.EXIT)

    def test_session_charge_on_exit(self):
        entry_result = authorize_plate(self.entry, "ABC123")
        self.assertTrue(entry_result.granted)

        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=2, minutes=5)
        session.save(update_fields=["start_time"])

        # 3 ceil hours * $2 + $1 additional = $7
        expected_fee = Decimal("7.00")
        exit_result = authorize_plate(self.exit, "ABC123")
        self.assertTrue(exit_result.granted)

        session.refresh_from_db()
        self.assertIsNotNone(session.end_time)
        self.assertEqual(session.payment_status, Session.PaymentStatus.PAID)
        self.assertEqual(session.fee, expected_fee)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("13.00"))
        self.assertTrue(
            Transaction.objects.filter(
                wallet=self.wallet,
                transaction_type=Transaction.Type.PARKING_FEE,
                session=session,
            ).exists()
        )

    def test_exit_denied_when_balance_below_fee(self):
        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=2)
        session.save(update_fields=["start_time"])

        # 2h → 2 hours * $2 + $1 = $5; leave only $4
        self.wallet.balance = Decimal("4.00")
        self.wallet.save(update_fields=["balance"])

        exit_result = authorize_plate(self.exit, "ABC123")
        self.assertFalse(exit_result.granted)
        self.assertIsNotNone(exit_result.access_request)
        self.assertEqual(
            exit_result.access_request.status, AccessRequest.Status.PENDING
        )
        self.assertEqual(exit_result.access_request.plate, "ABC123")

        session.refresh_from_db()
        self.assertIsNone(session.end_time)
        self.assertEqual(session.payment_status, Session.PaymentStatus.PENDING)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("4.00"))

    def test_entry_denied_when_below_minimum(self):
        self.wallet.balance = Decimal("1.00")
        self.wallet.save(update_fields=["balance"])
        result = authorize_plate(self.entry, "ABC123")
        self.assertFalse(result.granted)
        self.assertIsNotNone(result.access_request)
        self.assertEqual(result.access_request.status, AccessRequest.Status.PENDING)
        self.assertFalse(
            Session.objects.filter(plate="ABC123", end_time__isnull=True).exists()
        )

    def test_device_delete_publishes_remove(self):
        with patch("sites.signals.publish_once", return_value=True) as mock_pub:
            pk = self.entry.pk
            topic = self.entry.get_config_topic()
            self.entry.delete()
            mock_pub.assert_called()
            args, _kwargs = mock_pub.call_args
            self.assertEqual(args[0], topic)
            self.assertEqual(args[1]["action"], "remove")
            self.assertEqual(args[1]["enabled"], False)
            self.assertEqual(args[1]["camera_id"], pk)

    def test_fee_calculation_ceil_hours(self):
        start = timezone.now()
        end = start + timedelta(minutes=1)
        self.assertEqual(
            calculate_parking_fee(start, end, site=self.site),
            self.site.hourly_rate + self.site.additional_fee,
        )
        end = start + timedelta(hours=1, minutes=1)
        self.assertEqual(
            calculate_parking_fee(start, end, site=self.site),
            (self.site.hourly_rate * 2) + self.site.additional_fee,
        )

    def test_session_estimated_fee_matches_billing(self):
        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=1)
        session.save(update_fields=["start_time"])
        self.assertEqual(
            session.estimated_fee(),
            self.site.hourly_rate + self.site.additional_fee,
        )
        self.assertTrue(session.wallet_can_cover(self.wallet))

    def test_site_custom_rates_used_for_billing(self):
        zone = self.entry.zone
        zone.hourly_rate = Decimal("5.00")
        zone.additional_fee = Decimal("2.50")
        zone.save(update_fields=["hourly_rate", "additional_fee"])

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=1, minutes=1)
        session.save(update_fields=["start_time"])

        # 2 ceil hours * $5 + $2.50 = $12.50
        expected = Decimal("12.50")
        self.assertEqual(session.estimated_fee(), expected)
        self.assertEqual(zone.minimum_entry_balance(), Decimal("7.50"))
