"""Nested zones, visit grace, same-gate reopen, stack billing."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from parking.models import Event, Session
from parking.services import authorize_plate
from projects.models import Project
from sites.models import Device, Site, Zone
from wallets.models import Vehicle, Wallet


class NestedZoneBillingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Zones",
            wallet_enabled=True,
            pay_at_exit_enabled=False,
            grace_minutes=0,
        )
        self.site = Site.objects.create(
            project=self.project,
            name="Mall",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
            grace_minutes=30,
        )
        self.z1 = Zone.objects.get(site=self.site, name="Main")
        self.z1.name = "Outer"
        self.z1.hourly_rate = Decimal("2.00")
        self.z1.additional_fee = Decimal("1.00")
        self.z1.save()

        self.z2 = Zone.objects.create(
            site=self.site,
            parent=self.z1,
            name="Inner",
            hourly_rate=Decimal("5.00"),
            additional_fee=Decimal("2.00"),
        )

        with patch("sites.signals.publish_once", return_value=True):
            self.entry_z1 = Device.objects.create(
                zone=self.z1,
                ip="10.0.0.1",
                username="a",
                password="p",
                type=Device.Type.ENTRY,
            )
            self.exit_z1 = Device.objects.create(
                zone=self.z1,
                ip="10.0.0.2",
                username="a",
                password="p",
                type=Device.Type.EXIT,
            )
            self.entry_z2 = Device.objects.create(
                zone=self.z2,
                ip="10.0.0.3",
                username="a",
                password="p",
                type=Device.Type.ENTRY,
            )
            self.exit_z2 = Device.objects.create(
                zone=self.z2,
                ip="10.0.0.4",
                username="a",
                password="p",
                type=Device.Type.EXIT,
            )

        self.wallet = Wallet.objects.create(
            project=self.project, name="C", is_active=True
        )
        Vehicle.objects.create(wallet=self.wallet, plate="NEST01", is_active=True)
        self.wallet.balance = Decimal("100.00")
        self.wallet.save(update_fields=["balance"])

    @patch("parking.services.publish_once", return_value=True)
    def test_visit_grace_free_session(self, _pub):
        self.site.grace_minutes = 60
        self.site.save(update_fields=["grace_minutes"])
        self.assertTrue(authorize_plate(self.entry_z1, "NEST01").granted)
        session = Session.objects.get(plate="NEST01", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(minutes=10)
        session.save(update_fields=["start_time"])

        self.assertTrue(authorize_plate(self.exit_z1, "NEST01").granted)
        session.refresh_from_db()
        self.assertEqual(session.payment_status, Session.PaymentStatus.EXEMPTED)
        self.assertEqual(session.fee, Decimal("0.00"))

    @patch("parking.services.publish_once", return_value=True)
    def test_same_gate_reopen_no_new_session(self, _pub):
        self.assertTrue(authorize_plate(self.entry_z1, "NEST01").granted)
        self.assertEqual(Session.objects.filter(plate="NEST01", end_time__isnull=True).count(), 1)
        self.assertTrue(authorize_plate(self.entry_z1, "NEST01").granted)
        self.assertEqual(Session.objects.filter(plate="NEST01", end_time__isnull=True).count(), 1)
        self.assertEqual(Event.objects.filter(plate="NEST01", action="entry").count(), 2)

    @patch("parking.services.publish_once", return_value=True)
    def test_nested_stack_charges_sum_then_outer_grace(self, _pub):
        self.assertTrue(authorize_plate(self.entry_z1, "NEST01").granted)
        s1 = Session.objects.get(plate="NEST01", zone=self.z1, end_time__isnull=True)
        s1.start_time = timezone.now() - timedelta(hours=2)
        s1.save(update_fields=["start_time"])

        self.assertTrue(authorize_plate(self.entry_z2, "NEST01").granted)
        s2 = Session.objects.get(plate="NEST01", zone=self.z2, end_time__isnull=True)
        self.assertEqual(s2.parent_session_id, s1.pk)
        s2.start_time = timezone.now() - timedelta(hours=1)
        s2.save(update_fields=["start_time"])

        before = self.wallet.balance
        self.assertTrue(authorize_plate(self.exit_z2, "NEST01").granted)
        s2.refresh_from_db()
        s1.refresh_from_db()
        self.assertIsNotNone(s2.end_time)
        self.assertIsNone(s1.end_time)
        self.assertIsNotNone(s1.grace_exit_until)
        # Outer 2h: 2*2+1=5; Inner 1h: 5+2=7; total 12
        self.assertEqual(s2.fee, Decimal("12.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, before - Decimal("12.00"))

        self.assertTrue(authorize_plate(self.exit_z1, "NEST01").granted)
        s1.refresh_from_db()
        self.assertEqual(s1.payment_status, Session.PaymentStatus.EXEMPTED)
        self.assertEqual(s1.fee, Decimal("0.00"))
